"""Test-plan model and YAML loader.

A plan file looks like::

    name: DC Power Supply Regulation
    version: "1.0"
    description: Output accuracy, load regulation and current limit.
    on_fail: continue            # or: abort
    instruments:
      psu:
        resource: TCPIP0::192.168.1.20::inst0::INSTR
        timeout_ms: 5000
    variables:
      v_set: 5.0
    setup:
      - {instrument: psu, write: "*RST"}
    steps:
      - name: Output voltage
        instrument: psu
        query: "MEAS:VOLT?"
        parse: float
        units: V
        limits: {nominal: "{v_set}", tol_pct: 1}
        save_as: v_out
    teardown:
      - {instrument: psu, write: "OUTP OFF"}
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from .limits import KNOWN_LIMIT_KEYS, LimitError, Limits

__all__ = ["Plan", "Step", "InstrumentSpec", "PlanError", "load_plan", "plan_from_dict", "validate_plan"]

PARSE_MODES = ("auto", "float", "int", "str", "bool")
ON_FAIL_MODES = ("continue", "abort")
STEP_KEYS = {
    "name", "instrument", "write", "query", "expr", "delay_ms", "parse", "scale",
    "units", "limits", "save_as", "retries", "retry_delay_ms", "on_fail", "skip_if", "note",
}
INSTRUMENT_KEYS = {
    "resource", "timeout_ms", "read_termination", "write_termination", "query_delay",
    "check_errors", "visa_library", "baud_rate", "sim", "description",
}
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PlanError(ValueError):
    """Raised when a plan file is malformed."""


@dataclass
class InstrumentSpec:
    alias: str
    resource: str
    timeout_ms: int = 5000
    read_termination: str = "\n"
    write_termination: str = "\n"
    query_delay: float = 0.0
    check_errors: bool = False
    visa_library: str = ""
    baud_rate: Optional[int] = None
    description: str = ""
    sim: Optional[dict] = None

    @property
    def is_sim(self) -> bool:
        return self.resource.lower().startswith("sim://")


@dataclass
class Step:
    name: str
    instrument: Optional[str] = None
    write: Optional[str] = None
    query: Optional[str] = None
    expr: Optional[str] = None
    delay_ms: Optional[float] = None
    parse: str = "auto"
    scale: float = 1.0
    units: str = ""
    limits: Optional[dict] = None
    save_as: Optional[str] = None
    retries: int = 0
    retry_delay_ms: float = 200.0
    on_fail: Optional[str] = None
    skip_if: Optional[str] = None
    note: str = ""
    auto_name: bool = False

    @property
    def kind(self) -> str:
        if self.query is not None:
            return "query"
        if self.write is not None:
            return "write"
        if self.expr is not None:
            return "expr"
        return "delay"

    @property
    def has_limits(self) -> bool:
        return bool(self.limits)


@dataclass
class Plan:
    name: str
    version: str = ""
    description: str = ""
    on_fail: str = "continue"
    instruments: dict[str, InstrumentSpec] = field(default_factory=dict)
    variables: dict[str, Any] = field(default_factory=dict)
    setup: list[Step] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    teardown: list[Step] = field(default_factory=list)
    source: str = ""

    @property
    def slug(self) -> str:
        s = re.sub(r"[^A-Za-z0-9]+", "-", self.name).strip("-").lower()
        return s or "plan"


# ---------------------------------------------------------------- helpers

def _coerce(raw: dict, key: str, cast: Callable[[Any], Any], default: Any, where: str,
            minimum: Optional[float] = None) -> Any:
    """Read raw[key] through ``cast`` with a readable error instead of a traceback."""
    val = raw.get(key, default)
    if val is None:
        return default
    if isinstance(val, bool) and cast is not bool:
        raise PlanError(f"{where}: '{key}' must be a number, got {val!r}")
    try:
        out = cast(val)
    except (TypeError, ValueError):
        raise PlanError(f"{where}: '{key}' must be {cast.__name__}, got {val!r}") from None
    if minimum is not None and out < minimum:
        raise PlanError(f"{where}: '{key}' cannot be less than {minimum}")
    return out


def _unescape(s: Any) -> str:
    """Allow "\\n" / "\\r\\n" written literally in YAML."""
    if s is None:
        return ""
    return str(s).replace("\\r", "\r").replace("\\n", "\n")


# ---------------------------------------------------------------- building

def _step_from_dict(raw: Any, where: str) -> Step:
    if isinstance(raw, str):
        raise PlanError(f"{where}: a step must be a mapping, got the string {raw!r} "
                        f"(did you mean '- {{write: \"{raw}\"}}'?)")
    if not isinstance(raw, dict):
        raise PlanError(f"{where}: a step must be a mapping, got {type(raw).__name__}")
    unknown = sorted(set(raw) - STEP_KEYS)
    if unknown:
        raise PlanError(f"{where}: unknown key(s) {', '.join(unknown)}; "
                        f"allowed: {', '.join(sorted(STEP_KEYS))}")
    actions = [k for k in ("write", "query", "expr", "delay_ms") if raw.get(k) is not None]
    if len(actions) != 1:
        raise PlanError(f"{where}: a step needs exactly one of write/query/expr/delay_ms "
                        f"(found: {', '.join(actions) or 'none'})")
    name = raw.get("name")
    auto_name = not name
    if auto_name:
        action = actions[0]
        name = f"{action} {raw[action]}" if action != "delay_ms" else f"delay {raw['delay_ms']} ms"
    step = Step(
        name=str(name),
        auto_name=auto_name,
        instrument=None if raw.get("instrument") is None else str(raw["instrument"]),
        write=None if raw.get("write") is None else str(raw["write"]),
        query=None if raw.get("query") is None else str(raw["query"]),
        expr=None if raw.get("expr") is None else str(raw["expr"]),
        delay_ms=_coerce(raw, "delay_ms", float, None, where, minimum=0),
        parse=str(raw.get("parse", "auto") or "auto").lower(),
        scale=_coerce(raw, "scale", float, 1.0, where),
        units=str(raw.get("units", "") or ""),
        limits=raw.get("limits"),
        save_as=None if raw.get("save_as") is None else str(raw["save_as"]),
        retries=_coerce(raw, "retries", int, 0, where, minimum=0),
        retry_delay_ms=_coerce(raw, "retry_delay_ms", float, 200.0, where, minimum=0),
        on_fail=None if raw.get("on_fail") is None else str(raw["on_fail"]).lower(),
        skip_if=None if raw.get("skip_if") is None else str(raw["skip_if"]),
        note=str(raw.get("note", "") or ""),
    )
    if step.parse not in PARSE_MODES:
        raise PlanError(f"{where}: parse must be one of {', '.join(PARSE_MODES)}")
    if step.on_fail is not None and step.on_fail not in ON_FAIL_MODES:
        raise PlanError(f"{where}: on_fail must be one of {', '.join(ON_FAIL_MODES)}")
    if step.limits is not None:
        if not isinstance(step.limits, dict):
            raise PlanError(f"{where}: limits must be a mapping")
        bad = sorted(set(step.limits) - set(KNOWN_LIMIT_KEYS))
        if bad:
            raise PlanError(f"{where}: unknown limit key(s) {', '.join(bad)}; "
                            f"allowed: {', '.join(KNOWN_LIMIT_KEYS)}")
    if step.save_as is not None and not _IDENT.match(step.save_as):
        raise PlanError(f"{where}: save_as must be a valid identifier, got {step.save_as!r}")
    if step.kind in ("write", "query") and not step.instrument:
        raise PlanError(f"{where}: write/query steps need an 'instrument'")
    if step.kind in ("expr", "delay") and step.instrument:
        raise PlanError(f"{where}: expr/delay steps must not name an instrument")
    if step.kind == "write" and step.limits:
        raise PlanError(f"{where}: write steps cannot have limits (use a query)")
    if step.kind == "delay" and (step.limits or step.save_as):
        raise PlanError(f"{where}: delay steps cannot have limits or save_as")
    return step


def _instrument_from_dict(alias: str, raw: Any) -> InstrumentSpec:
    where = f"instrument '{alias}'"
    if isinstance(raw, str):
        raw = {"resource": raw}
    if not isinstance(raw, dict):
        raise PlanError(f"{where} must be a mapping or a resource string")
    unknown = sorted(set(raw) - INSTRUMENT_KEYS)
    if unknown:
        raise PlanError(f"{where}: unknown key(s) {', '.join(unknown)}; "
                        f"allowed: {', '.join(sorted(INSTRUMENT_KEYS))}")
    resource = raw.get("resource")
    if not resource or not isinstance(resource, str):
        raise PlanError(f"{where} needs a 'resource' (VISA string or sim://name)")
    if not _IDENT.match(alias):
        raise PlanError(f"instrument alias {alias!r} must be a valid identifier")
    spec = InstrumentSpec(
        alias=alias,
        resource=resource.strip(),
        timeout_ms=_coerce(raw, "timeout_ms", int, 5000, where, minimum=1),
        read_termination=_unescape(raw.get("read_termination", "\n")),
        write_termination=_unescape(raw.get("write_termination", "\n")),
        query_delay=_coerce(raw, "query_delay", float, 0.0, where, minimum=0),
        check_errors=bool(raw.get("check_errors", False)),
        visa_library=str(raw.get("visa_library", "") or ""),
        baud_rate=_coerce(raw, "baud_rate", int, None, where, minimum=1),
        description=str(raw.get("description", "") or ""),
        sim=raw.get("sim"),
    )
    if spec.sim is not None and not isinstance(spec.sim, dict):
        raise PlanError(f"{where}: sim must be a mapping")
    if spec.sim is not None:
        for key in ("state", "derived", "commands"):
            if key in spec.sim and spec.sim[key] is not None and not isinstance(spec.sim[key], dict):
                raise PlanError(f"{where}: sim.{key} must be a mapping")
    return spec


def plan_from_dict(raw: Any, source: str = "") -> Plan:
    if not isinstance(raw, dict):
        raise PlanError("plan must be a mapping at the top level")
    name = raw.get("name")
    if not name:
        raise PlanError("plan needs a 'name'")
    on_fail = str(raw.get("on_fail", "continue") or "continue").lower()
    if on_fail not in ON_FAIL_MODES:
        raise PlanError(f"on_fail must be one of {', '.join(ON_FAIL_MODES)}")

    raw_instruments = raw.get("instruments") or {}
    if not isinstance(raw_instruments, dict):
        raise PlanError("instruments must be a mapping of alias -> resource/settings")
    instruments: dict[str, InstrumentSpec] = {}
    for alias, spec in raw_instruments.items():
        instruments[str(alias)] = _instrument_from_dict(str(alias), spec)

    variables = raw.get("variables") or {}
    if not isinstance(variables, dict):
        raise PlanError("variables must be a mapping")
    for k in variables:
        if not _IDENT.match(str(k)):
            raise PlanError(f"variable name {k!r} must be a valid identifier")

    def steps(section: str) -> list[Step]:
        items = raw.get(section) or []
        if not isinstance(items, list):
            raise PlanError(f"'{section}' must be a list of steps")
        out = []
        for i, item in enumerate(items, 1):
            step = _step_from_dict(item, f"{section}[{i}]")
            if step.instrument and step.instrument not in instruments:
                raise PlanError(f"{section}[{i}] ('{step.name}'): unknown instrument "
                                f"'{step.instrument}'; defined: {', '.join(instruments) or 'none'}")
            out.append(step)
        return out

    plan = Plan(
        name=str(name),
        version=str(raw.get("version", "") or ""),
        description=str(raw.get("description", "") or ""),
        on_fail=on_fail,
        instruments=instruments,
        variables=dict(variables),
        setup=steps("setup"),
        steps=steps("steps"),
        teardown=steps("teardown"),
        source=source,
    )
    if not plan.steps:
        raise PlanError("plan has no 'steps'")
    return plan


def load_plan(path: str | Path) -> Plan:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PlanError(f"cannot read {path}: {exc}") from None
    except UnicodeDecodeError:
        raise PlanError(f"{path} is not UTF-8 encoded (save the file as UTF-8)") from None
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PlanError(f"{path}: YAML error: {exc}") from None
    return plan_from_dict(raw, source=str(path))


def validate_plan(path: str | Path) -> list[str]:
    """Return a list of problems (empty list means the plan is valid)."""
    try:
        plan = load_plan(path)
    except PlanError as exc:
        return [str(exc)]
    problems: list[str] = []
    seen: set[str] = set()
    for step in plan.steps:
        if step.has_limits and step.name in seen:
            problems.append(f"duplicate step name '{step.name}' (results will be ambiguous)")
        seen.add(step.name)
        if step.limits:
            try:
                # static check: literal numeric limits must be coherent
                Limits.from_spec(step.limits, resolve=_static_number, resolve_literal=lambda v: v)
            except LimitError as exc:
                problems.append(f"step '{step.name}': {exc}")
            except _Dynamic:
                pass  # depends on variables/measurements; checked at run time
    return problems


class _Dynamic(Exception):
    pass


def _static_number(v: Any) -> Any:
    if isinstance(v, bool):
        raise _Dynamic
    if isinstance(v, (int, float)):
        return v
    try:
        return float(str(v))
    except ValueError:
        raise _Dynamic from None
