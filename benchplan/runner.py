"""Executes a plan against its instruments and collects results."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .instruments import BaseInstrument, InstrumentError, SimGroups, open_instrument, sleep_ms
from .limits import LimitError, Limits
from .plan import InstrumentSpec, Plan, Step
from .safe_eval import ExprError, safe_eval

__all__ = ["Runner", "RunResult", "StepResult", "run_plan", "STATUS_ORDER"]

PASS, FAIL, ERROR, SKIP, INFO, DONE = "PASS", "FAIL", "ERROR", "SKIP", "INFO", "DONE"
STATUS_ORDER = (PASS, FAIL, ERROR, SKIP, INFO, DONE)


@dataclass
class StepResult:
    index: int
    phase: str                      # setup | test | teardown
    name: str
    kind: str                       # query | write | expr | delay
    status: str                     # PASS | FAIL | ERROR | SKIP | INFO | DONE
    instrument: str = ""
    command: str = ""
    raw: str = ""                   # raw instrument response
    value: Any = None               # parsed/scaled value
    units: str = ""
    limits: str = ""                # human-readable limits
    message: str = ""
    attempts: int = 1
    duration_ms: float = 0.0
    timestamp: str = ""
    note: str = ""

    @property
    def is_test(self) -> bool:
        return self.status in (PASS, FAIL)


@dataclass
class RunResult:
    run_id: str
    plan_name: str
    plan_version: str
    plan_description: str
    plan_source: str
    serial: str
    operator: str
    started_at: str
    finished_at: str = ""
    duration_s: float = 0.0
    status: str = ERROR
    steps: list[StepResult] = field(default_factory=list)
    variables: dict[str, Any] = field(default_factory=dict)
    measurements: dict[str, Any] = field(default_factory=dict)
    instruments: dict[str, str] = field(default_factory=dict)   # alias -> *IDN? (or resource)
    aborted: bool = False
    error: str = ""

    @property
    def counts(self) -> dict[str, int]:
        c = {s: 0 for s in STATUS_ORDER}
        for s in self.steps:
            c[s.status] = c.get(s.status, 0) + 1
        return c

    @property
    def tests(self) -> list[StepResult]:
        return [s for s in self.steps if s.phase == "test" and s.status in (PASS, FAIL, ERROR, SKIP)]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["counts"] = self.counts
        return d


class _Missing(dict):
    def __missing__(self, key):
        raise KeyError(key)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Runner:
    """Run a :class:`Plan`.

    ``on_step`` is called with each :class:`StepResult` as it completes
    (used for live console output). ``open_fn(spec)`` may be supplied to
    plug in custom instrument objects (anything with ``write``/``query``/``close``).
    """

    def __init__(self, plan: Plan, *, serial: str = "", operator: str = "",
                 variables: Optional[dict] = None, on_fail: Optional[str] = None,
                 on_step: Optional[Callable[[StepResult], None]] = None,
                 open_fn: Optional[Callable[[InstrumentSpec], BaseInstrument]] = None):
        self.plan = plan
        self.serial = serial
        self.operator = operator
        self.on_fail = on_fail or plan.on_fail
        self.on_step = on_step or (lambda r: None)
        self.sim_groups: SimGroups = {}            # simulated shared state lives per run
        self.open_fn = open_fn or self._open_default
        self.variables: dict[str, Any] = dict(plan.variables)
        self.variables.update(variables or {})
        self.variables.setdefault("serial", serial)
        self.variables.setdefault("operator", operator)
        self.measurements: dict[str, Any] = {}
        self.instruments: dict[str, BaseInstrument] = {}
        self._index = 0

    def _open_default(self, spec: InstrumentSpec) -> BaseInstrument:
        return open_instrument(spec, self.sim_groups)

    # ------------------------------------------------------------ helpers
    def env(self) -> dict[str, Any]:
        e = dict(self.variables)
        e.update(self.measurements)
        return e

    def substitute(self, text: str) -> str:
        """Replace ``{name}`` placeholders with variables/measurements."""
        try:
            return str(text).format_map(_Missing(self.env()))
        except KeyError as exc:
            raise ExprError(f"unknown placeholder {{{exc.args[0]}}} in {text!r}") from None
        except (ValueError, IndexError) as exc:
            raise ExprError(f"bad placeholder syntax in {text!r}: {exc}") from None

    def resolve_number(self, value: Any) -> Any:
        """Resolve a numeric limit value: number, '{placeholder}' or expression."""
        if isinstance(value, bool) or value is None:
            return value
        if isinstance(value, (int, float)):
            return value
        text = self.substitute(str(value))
        try:
            return float(text)
        except ValueError:
            return safe_eval(text, self.env())

    def resolve_literal(self, value: Any) -> Any:
        """Substitute placeholders in a literal limit (equals/one_of/regex); never evaluates."""
        if isinstance(value, str):
            return self.substitute(value)
        return value

    def _parse(self, raw: str, step: Step) -> Any:
        text = raw.strip()
        mode = step.parse
        if mode == "str":
            return text.strip('"')
        if mode == "bool":
            return text.strip().strip('"').upper() in ("1", "ON", "TRUE", "YES", "+1")
        if mode in ("float", "int", "auto"):
            try:
                num = float(text)
            except ValueError:
                if mode == "auto":
                    return text.strip('"')
                raise ValueError(f"cannot parse {raw!r} as {mode}") from None
            num *= step.scale
            if mode == "int":
                return int(round(num))
            return num
        return text

    # ------------------------------------------------------------ running
    def run(self) -> RunResult:
        t0 = time.perf_counter()
        result = RunResult(
            run_id=uuid.uuid4().hex[:12],
            plan_name=self.plan.name, plan_version=self.plan.version,
            plan_description=self.plan.description, plan_source=self.plan.source,
            serial=self.serial, operator=self.operator, started_at=_now_iso(),
            variables=dict(self.variables),
        )
        self._index = 0
        self.measurements = {}
        self.sim_groups.clear()
        try:
            self._open_all(result)
            try:
                self._run_section(self.plan.setup, "setup", result)
                if not result.aborted:
                    self._run_section(self.plan.steps, "test", result)
            finally:
                self._run_section(self.plan.teardown, "teardown", result, force=True)
        except InstrumentError as exc:
            result.error = str(exc)
        except Exception as exc:  # pragma: no cover - defensive
            result.error = f"{type(exc).__name__}: {exc}"
        finally:
            self._close_all()
        result.measurements = dict(self.measurements)
        result.duration_s = round(time.perf_counter() - t0, 3)
        result.finished_at = _now_iso()
        result.status = self._overall(result)
        return result

    def _open_all(self, result: RunResult) -> None:
        for alias, spec in self.plan.instruments.items():
            inst = self.open_fn(spec)
            self.instruments[alias] = inst
            ident = spec.resource
            if hasattr(inst, "idn"):
                try:
                    ident = str(inst.idn())
                except Exception:
                    pass
            result.instruments[alias] = ident

    def _close_all(self) -> None:
        for inst in self.instruments.values():
            try:
                inst.close()
            except Exception:
                pass
        self.instruments.clear()

    def _notify(self, sr: StepResult) -> None:
        try:
            self.on_step(sr)
        except Exception:
            pass  # a logging/console problem must never abort a test run

    def _run_section(self, steps: list[Step], phase: str, result: RunResult, force: bool = False) -> None:
        for step in steps:
            self._index += 1
            if result.aborted and not force:
                sr = StepResult(self._index, phase, step.name, step.kind, SKIP,
                                instrument=step.instrument or "", message="skipped after abort",
                                units=step.units, timestamp=_now_iso(), note=step.note)
                result.steps.append(sr)
                self._notify(sr)
                continue
            sr = self._exec(step, phase)
            sr.index = self._index
            result.steps.append(sr)
            self._notify(sr)
            mode = step.on_fail or self.on_fail
            if sr.status in (FAIL, ERROR) and mode == "abort" and phase != "teardown":
                result.aborted = True

    def _exec(self, step: Step, phase: str) -> StepResult:
        sr = StepResult(0, phase, step.name, step.kind, DONE, instrument=step.instrument or "",
                        units=step.units, timestamp=_now_iso(), note=step.note)
        t0 = time.perf_counter()
        try:
            if step.skip_if:
                if safe_eval(self.substitute(step.skip_if), self.env()):
                    sr.status, sr.message = SKIP, f"skip_if: {step.skip_if}"
                    return sr
            limits = Limits.from_spec(step.limits, self.resolve_number, self.resolve_literal) \
                if step.limits else None
            if limits is not None:
                sr.limits = limits.describe(step.units)
            attempts = step.retries + 1
            for attempt in range(1, attempts + 1):
                sr.attempts = attempt
                try:
                    self._do(step, sr, limits)
                except (InstrumentError, ValueError, ExprError) as exc:
                    sr.status, sr.message = ERROR, str(exc)
                except Exception as exc:  # anything else is still a step error, not a crash
                    sr.status, sr.message = ERROR, f"{type(exc).__name__}: {exc}"
                if sr.status not in (FAIL, ERROR) or attempt == attempts:
                    break
                sleep_ms(step.retry_delay_ms)
            if sr.attempts > 1 and sr.status not in (FAIL, ERROR):
                sr.message = f"{sr.message} (passed on attempt {sr.attempts})".strip()
        except (LimitError, ExprError) as exc:
            sr.status, sr.message = ERROR, str(exc)
        except Exception as exc:
            sr.status, sr.message = ERROR, f"{type(exc).__name__}: {exc}"
        finally:
            sr.duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        return sr

    def _do(self, step: Step, sr: StepResult, limits: Optional[Limits]) -> None:
        sr.status, sr.message, sr.raw, sr.value = DONE, "", "", None
        if step.kind == "delay":
            sleep_ms(step.delay_ms or 0)
            sr.message = f"waited {step.delay_ms:g} ms"
            return
        if step.kind == "write":
            cmd = self.substitute(step.write)
            sr.command = cmd
            if step.auto_name:
                sr.name = f"write {cmd}"
            self.instruments[step.instrument].write(cmd)
            sr.message = "sent"
            return
        if step.kind == "query":
            cmd = self.substitute(step.query)
            sr.command = cmd
            if step.auto_name:
                sr.name = f"query {cmd}"
            raw = self.instruments[step.instrument].query(cmd)
            sr.raw = str(raw)
            value = self._parse(sr.raw, step)
        else:  # expr
            text = self.substitute(step.expr)
            sr.command = text
            value = safe_eval(text, self.env())
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                value = value * step.scale
                if step.parse == "int":
                    value = int(round(value))
        sr.value = value
        if step.save_as:
            self.measurements[step.save_as] = value
        if limits is None or limits.is_empty():
            sr.status, sr.message = INFO, "recorded"
            return
        ok, reason = limits.check(value)
        sr.status, sr.message = (PASS if ok else FAIL), reason

    @staticmethod
    def _overall(result: RunResult) -> str:
        if result.error:
            return ERROR
        statuses = [s.status for s in result.steps if s.phase != "teardown" or s.status != SKIP]
        if ERROR in statuses:
            return ERROR
        if FAIL in statuses:
            return FAIL
        if result.aborted:
            return FAIL
        return PASS


def run_plan(plan: Plan, **kwargs) -> RunResult:
    """Convenience wrapper: ``run_plan(plan, serial=..., operator=...)``."""
    return Runner(plan, **kwargs).run()
