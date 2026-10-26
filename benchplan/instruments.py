"""Instrument backends.

* ``VisaInstrument`` – any SCPI instrument reachable through PyVISA
  (LAN/VXI-11, USBTMC, GPIB, serial). ``pip install benchplan[visa]``.
* ``SimInstrument`` – an in-process simulated instrument described in the
  plan file itself, so plans can be developed, demoed and unit-tested
  with no hardware attached.

Simulated instrument block (inside ``instruments.<alias>.sim``)::

    sim:
      state_group: bench      # optional: instruments in the same group share state
      seed: 1                 # optional: deterministic noise
      state:                  # initial state variables
        v_set: 0.0
        out: 0
      derived:                # recomputed before every response, in order
        v_out: "v_set if out else 0.0"
      commands:
        "VOLT (?P<v>[-\\d.]+)":   {set: {v_set: "v"}}
        "OUTP (?P<s>ON|OFF)":     {set: {out: "1 if s == 'ON' else 0"}}
        "VOLT\\?":                "{v_set}"
        "MEAS:VOLT\\?":           {response: "v_out + gauss(0, 0.001)"}

Command patterns are regular expressions matched case-insensitively against
the whole command. Named groups become variables for ``set``/``response``
expressions (converted to numbers when possible).
"""
from __future__ import annotations

import random
import re
import time
from typing import Any, Optional

from .plan import InstrumentSpec
from .safe_eval import ExprError, default_funcs, safe_eval

__all__ = ["BaseInstrument", "SimInstrument", "VisaInstrument", "InstrumentError", "open_instrument"]


class InstrumentError(RuntimeError):
    """Communication or protocol error talking to an instrument."""


class BaseInstrument:
    def __init__(self, spec: InstrumentSpec):
        self.spec = spec
        self.alias = spec.alias
        self.log: list[tuple[str, str, str]] = []  # (direction, command, response)

    def open(self) -> None: ...
    def close(self) -> None: ...

    def write(self, command: str) -> None:
        raise NotImplementedError

    def query(self, command: str) -> str:
        raise NotImplementedError

    def idn(self) -> str:
        return self.query("*IDN?")

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()


# ----------------------------------------------------------------- simulated

class _SharedState:
    """State dict + RNG shared by simulated instruments in one state group."""

    def __init__(self, seed: Optional[int] = None):
        self.values: dict[str, Any] = {}
        self.initial: dict[str, Any] = {}
        self.rng = random.Random(seed)
        self.funcs = default_funcs(self.rng)
        self.derived: list[tuple[str, str]] = []


_state_groups: dict[str, _SharedState] = {}


def reset_sim_groups() -> None:
    """Forget all shared simulation state (used between runs/tests)."""
    _state_groups.clear()


def _to_number(s: str) -> Any:
    try:
        if re.fullmatch(r"[-+]?\d+", s):
            return int(s)
        return float(s)
    except (TypeError, ValueError):
        return s


class SimInstrument(BaseInstrument):
    DEFAULT_COMMANDS = {
        r"\*IDN\?": "BENCHPLAN,SIM-{__alias__},0,0.1",
        r"SYST:ERR\?": '0,"No error"',
        r"\*OPC\?": "1",
        r"\*RST": {"reset": True},
        r"\*CLS": {},
    }

    def __init__(self, spec: InstrumentSpec):
        super().__init__(spec)
        sim = spec.sim or {}
        group = str(sim.get("state_group") or spec.alias)
        seed = sim.get("seed")
        shared = _state_groups.get(group)
        if shared is None:
            shared = _SharedState(seed if seed is None else int(seed))
            _state_groups[group] = shared
        self.state = shared
        for k, v in (sim.get("state") or {}).items():
            self.state.initial.setdefault(k, v)
            self.state.values.setdefault(k, v)
        for k, expr in (sim.get("derived") or {}).items():
            if all(k != dk for dk, _ in self.state.derived):
                self.state.derived.append((k, str(expr)))
        self.rules: list[tuple[re.Pattern, Any]] = []
        for pattern, rule in (sim.get("commands") or {}).items():
            self.rules.append((re.compile(str(pattern), re.IGNORECASE), rule))
        for pattern, rule in self.DEFAULT_COMMANDS.items():
            self.rules.append((re.compile(pattern, re.IGNORECASE), rule))
        self.opened = False

    # -- lifecycle
    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.opened = False

    # -- helpers
    def _env(self, extra: Optional[dict] = None) -> dict[str, Any]:
        env = dict(self.state.values)
        env["__alias__"] = self.alias
        for key, expr in self.state.derived:
            try:
                env[key] = safe_eval(expr, env, self.state.funcs)
            except ExprError as exc:
                raise InstrumentError(f"[{self.alias}] derived '{key}': {exc}") from None
        if extra:
            env.update(extra)
        return env

    def _match(self, command: str) -> tuple[Any, dict]:
        cmd = command.strip()
        for pattern, rule in self.rules:
            m = pattern.fullmatch(cmd)
            if m:
                groups = {k: _to_number(v) for k, v in m.groupdict().items() if v is not None}
                return rule, groups
        raise KeyError(cmd)

    def _apply(self, rule: Any, groups: dict) -> Optional[str]:
        if isinstance(rule, str):
            env = self._env(groups)
            return rule.format_map(_FormatEnv(env))
        if rule is None:
            return None
        if not isinstance(rule, dict):
            return str(rule)
        if rule.get("reset"):
            self.state.values.update(self.state.initial)
        env = self._env(groups)
        for key, expr in (rule.get("set") or {}).items():
            try:
                self.state.values[key] = safe_eval(expr, env, self.state.funcs)
            except ExprError as exc:
                raise InstrumentError(f"[{self.alias}] set '{key}': {exc}") from None
            env[key] = self.state.values[key]
        if "response" not in rule:
            return None
        resp = rule["response"]
        if isinstance(resp, (int, float, bool)):
            return _fmt_number(resp)
        try:
            value = safe_eval(resp, env, self.state.funcs)
        except ExprError:
            # not an expression: treat as a format string
            return str(resp).format_map(_FormatEnv(env))
        return _fmt_number(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else str(value)

    # -- SCPI
    def write(self, command: str) -> None:
        self.log.append(("W", command, ""))
        try:
            rule, groups = self._match(command)
        except KeyError:
            return  # unknown writes are silently accepted, like most real instruments
        self._apply(rule, groups)

    def query(self, command: str) -> str:
        try:
            rule, groups = self._match(command)
        except KeyError:
            raise InstrumentError(f"[{self.alias}] simulated instrument has no response "
                                  f"for {command.strip()!r}") from None
        resp = self._apply(rule, groups)
        if resp is None:
            raise InstrumentError(f"[{self.alias}] {command.strip()!r} is a write-only command "
                                  f"in the simulation (no 'response')")
        self.log.append(("Q", command, resp))
        return resp


class _FormatEnv(dict):
    def __missing__(self, key):
        raise InstrumentError(f"unknown placeholder '{{{key}}}' in simulated response")


def _fmt_number(v: Any) -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    return f"{float(v):.6E}"  # SCPI-style exponent notation, e.g. 4.998000E+00


# ---------------------------------------------------------------------- VISA

class VisaInstrument(BaseInstrument):
    def __init__(self, spec: InstrumentSpec):
        super().__init__(spec)
        self._rm = None
        self._res = None

    def open(self) -> None:
        try:
            import pyvisa
        except ImportError:
            raise InstrumentError(
                "pyvisa is not installed. Install with:  pip install benchplan[visa]\n"
                "(pyvisa-py gives a pure-Python backend for LAN/USB/serial; or use NI-VISA / Keysight IO Libraries)"
            ) from None
        try:
            self._rm = pyvisa.ResourceManager(self.spec.visa_library) if self.spec.visa_library \
                else pyvisa.ResourceManager()
            self._res = self._rm.open_resource(self.spec.resource)
        except Exception as exc:  # pyvisa raises many different types
            raise InstrumentError(f"[{self.alias}] cannot open {self.spec.resource}: {exc}") from None
        self._res.timeout = self.spec.timeout_ms
        if self.spec.read_termination:
            self._res.read_termination = self.spec.read_termination
        if self.spec.write_termination:
            self._res.write_termination = self.spec.write_termination
        if self.spec.query_delay:
            self._res.query_delay = self.spec.query_delay

    def close(self) -> None:
        for obj in (self._res, self._rm):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        self._res = self._rm = None

    def _check(self) -> None:
        if not self.spec.check_errors:
            return
        try:
            err = self._res.query("SYST:ERR?").strip()
        except Exception as exc:
            raise InstrumentError(f"[{self.alias}] SYST:ERR? failed: {exc}") from None
        if not (err.startswith("0,") or err.startswith("+0,")):
            raise InstrumentError(f"[{self.alias}] instrument reported error: {err}")

    def write(self, command: str) -> None:
        if self._res is None:
            raise InstrumentError(f"[{self.alias}] instrument is not open")
        self.log.append(("W", command, ""))
        try:
            self._res.write(command)
        except Exception as exc:
            raise InstrumentError(f"[{self.alias}] write {command!r} failed: {exc}") from None
        self._check()

    def query(self, command: str) -> str:
        if self._res is None:
            raise InstrumentError(f"[{self.alias}] instrument is not open")
        try:
            resp = self._res.query(command).strip()
        except Exception as exc:
            raise InstrumentError(f"[{self.alias}] query {command!r} failed: {exc}") from None
        self.log.append(("Q", command, resp))
        self._check()
        return resp


def open_instrument(spec: InstrumentSpec) -> BaseInstrument:
    inst = SimInstrument(spec) if spec.is_sim else VisaInstrument(spec)
    inst.open()
    return inst


def list_resources(visa_library: str = "") -> list[str]:
    """List VISA resources visible to the default (or given) backend."""
    try:
        import pyvisa
    except ImportError:
        raise InstrumentError("pyvisa is not installed (pip install benchplan[visa])") from None
    rm = pyvisa.ResourceManager(visa_library) if visa_library else pyvisa.ResourceManager()
    try:
        return list(rm.list_resources())
    finally:
        rm.close()


def sleep_ms(ms: float) -> None:
    if ms and ms > 0:
        time.sleep(ms / 1000.0)
