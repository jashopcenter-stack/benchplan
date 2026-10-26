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
from typing import Any, Optional

import yaml

from .limits import KNOWN_LIMIT_KEYS

__all__ = ["Plan", "Step", "InstrumentSpec", "PlanError", "load_plan", "plan_from_dict", "validate_plan"]

PARSE_MODES = ("auto", "float", "int", "str", "bool")
ON_FAIL_MODES = ("continue", "abort")
STEP_KEYS = {
    "name", "instrument", "write", "query", "expr", "delay_ms", "parse", "scale",
    "units", "limits", "save_as", "retries", "retry_delay_ms", "on_fail", "skip_if",
    "note", "timeout_ms",
}
INSTRUMENT_KEYS = {
    "resource", "timeout_ms", "read_termination", "write_termination", "query_delay",
    "check_errors", "visa_library", "sim", "description",
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
    timeout_ms: Optional[int] = None
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
        instrument=raw.get("instrument"),
        write=None if raw.get("write") is None else str(raw["write"]),
        query=None if raw.get("query") is None else str(raw["query"]),
        expr=None if raw.get("expr") is None else str(raw["expr"]),
        delay_ms=None if raw.get("delay_ms") is None else float(raw["delay_ms"]),
        parse=str(raw.get("parse", "auto")).lower(),
        scale=float(raw.get("scale", 1.0)),
        units=str(raw.get("units", "") or ""),
        limits=raw.get("limits"),
        save_as=raw.get("save_as"),
        retries=int(raw.get("retries", 0) or 0),
        retry_delay_ms=float(raw.get("retry_delay_ms", 200.0)),
        on_fail=raw.get("on_fail"),
        skip_if=None if raw.get("skip_if") is None else str(raw["skip_if"]),
        note=str(raw.get("note", "") or ""),
        timeout_ms=None if raw.get("timeout_ms") is None else int(raw["timeout_ms"]),
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
    if step.save_as is not None and not _IDENT.match(str(step.save_as)):
        raise PlanError(f"{where}: save_as must be a valid identifier, got {step.save_as!r}")
    if step.retries < 0:
        raise PlanError(f"{where}: retries cannot be negative")
    if step.kind in ("write", "query") and not step.instrument:
        raise PlanError(f"{where}: write/query steps need an 'instrument'")
    if step.kind in ("expr", "delay") and step.instrument:
        raise PlanError(f"{where}: expr/delay steps must not name an instrument")
    if step.kind == "write" and step.limits:
        raise PlanError(f"{where}: write steps cannot have limits (use a query)")
    return step


def _instrument_from_dict(alias: str, raw: Any) -> InstrumentSpec:
    if isinstance(raw, str):
        raw = {"resource": raw}
    if not isinstance(raw, dict):
        raise PlanError(f"instrument '{alias}' must be a mapping or a resource string")
    unknown = sorted(set(raw) - INSTRUMENT_KEYS)
    if unknown:
        raise PlanError(f"instrument '{alias}': unknown key(s) {', '.join(unknown)}")
    resource = raw.get("resource")
    if not resource:
        raise PlanError(f"instrument '{alias}' needs a 'resource' (VISA string or sim://name)")
    if not _IDENT.match(alias):
        raise PlanError(f"instrument alias {alias!r} must be a valid identifier")
    spec = InstrumentSpec(
        alias=alias,
        resource=str(resource),
        timeout_ms=int(raw.get("timeout_ms", 5000)),
        read_termination=_unescape(raw.get("read_termination", "\n")),
        write_termination=_unescape(raw.get("write_termination", "\n")),
        query_delay=float(raw.get("query_delay", 0.0)),
        check_errors=bool(raw.get("check_errors", False)),
        visa_library=str(raw.get("visa_library", "") or ""),
        description=str(raw.get("description", "") or ""),
        sim=raw.get("sim"),
    )
    if spec.sim is not None and not isinstance(spec.sim, dict):
        raise PlanError(f"instrument '{alias}': sim must be a mapping")
    return spec


def _unescape(s: Any) -> str:
    """Allow "\\n" / "\\r\\n" written literally in YAML."""
    if s is None:
        return ""
    return str(s).replace("\\r", "\r").replace("\\n", "\n")


def plan_from_dict(raw: Any, source: str = "") -> Plan:
    if not isinstance(raw, dict):
        raise PlanError("plan must be a mapping at the top level")
    name = raw.get("name")
    if not name:
        raise PlanError("plan needs a 'name'")
    on_fail = str(raw.get("on_fail", "continue")).lower()
    if on_fail not in ON_FAIL_MODES:
        raise PlanError(f"on_fail must be one of {', '.join(ON_FAIL_MODES)}")

    instruments: dict[str, InstrumentSpec] = {}
    for alias, spec in (raw.get("instruments") or {}).items():
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
    return problems
