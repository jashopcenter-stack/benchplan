"""Pass/fail limit evaluation.

A ``limits`` block in a plan step may contain any combination of:

    min: 4.95            lower bound (inclusive)
    max: 5.05            upper bound (inclusive)
    nominal: 5.0         target value, used with ``tol`` and/or ``tol_pct``
    tol: 0.05            absolute tolerance around ``nominal`` (±)
    tol_pct: 1.0         percent tolerance around ``nominal`` (±)
    equals: "ON"         exact match (string or number; ON/OFF/TRUE/FALSE/1/0 are equivalent)
    one_of: [CV, CC]     value must be one of the listed options
    regex: "^RIGOL"      value (as text) must match this regular expression

Numeric values (min/max/nominal/tol/tol_pct) may be literal numbers,
``{placeholders}`` or expressions over plan variables and earlier
measurements, e.g. ``max: "v_set * 1.01"``.  ``equals``, ``one_of`` and
``regex`` are literals: ``{placeholders}`` are substituted but nothing is
evaluated, so ``equals: "{serial}"`` compares against the serial as text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

__all__ = ["Limits", "LimitError", "KNOWN_LIMIT_KEYS"]

KNOWN_LIMIT_KEYS = ("min", "max", "nominal", "tol", "tol_pct", "equals", "one_of", "regex")
NUMERIC_LIMIT_KEYS = ("min", "max", "nominal", "tol", "tol_pct")


class LimitError(ValueError):
    """Raised when a limits block is malformed."""


def _fmt(v: Any, units: str = "") -> str:
    """Format a number compactly, with optional units."""
    if isinstance(v, bool):
        s = str(v)
    elif isinstance(v, (int, float)):
        s = f"{v:.6g}"
    else:
        s = str(v)
    return f"{s} {units}".strip() if units else s


@dataclass
class Limits:
    min: Optional[float] = None
    max: Optional[float] = None
    nominal: Optional[float] = None
    tol: Optional[float] = None
    tol_pct: Optional[float] = None
    equals: Any = None
    one_of: Optional[list] = None
    regex: Optional[str] = None

    # ------------------------------------------------------------------ build
    @classmethod
    def from_spec(cls, spec: dict, resolve: Callable[[Any], Any] | None = None,
                  resolve_literal: Callable[[Any], Any] | None = None) -> "Limits":
        """Build from a plan dict.

        ``resolve`` turns numeric limit values (placeholders/expressions) into
        numbers; ``resolve_literal`` substitutes placeholders in
        ``equals``/``one_of``/``regex`` without evaluating anything.
        """
        if not isinstance(spec, dict):
            raise LimitError(f"limits must be a mapping, got {type(spec).__name__}")
        unknown = [k for k in spec if k not in KNOWN_LIMIT_KEYS]
        if unknown:
            raise LimitError(f"unknown limit key(s): {', '.join(unknown)}; "
                             f"allowed: {', '.join(KNOWN_LIMIT_KEYS)}")
        resolve = resolve or (lambda v: v)
        resolve_literal = resolve_literal or (lambda v: v)
        kw: dict[str, Any] = {}
        for key in NUMERIC_LIMIT_KEYS:
            if key in spec and spec[key] is not None:
                val = resolve(spec[key])
                if isinstance(val, bool):
                    raise LimitError(f"limit '{key}' must be a number, got {val!r}")
                try:
                    kw[key] = float(val)
                except (TypeError, ValueError):
                    raise LimitError(f"limit '{key}' ({spec[key]!r}) must resolve to a number, got {val!r}") from None
                if kw[key] != kw[key]:
                    raise LimitError(f"limit '{key}' resolved to NaN")
        if "equals" in spec:
            kw["equals"] = resolve_literal(spec["equals"])
        if "one_of" in spec:
            opts = spec["one_of"]
            if not isinstance(opts, (list, tuple)) or not opts:
                raise LimitError("one_of must be a non-empty list")
            kw["one_of"] = [resolve_literal(o) for o in opts]
        if "regex" in spec:
            pattern = str(resolve_literal(spec["regex"]))
            try:
                re.compile(pattern)
            except re.error as exc:
                raise LimitError(f"invalid regex {pattern!r}: {exc}") from None
            kw["regex"] = pattern
        lim = cls(**kw)
        if (lim.tol is not None or lim.tol_pct is not None) and lim.nominal is None:
            raise LimitError("tol/tol_pct require a nominal value")
        if lim.nominal is not None and all(v is None for v in (lim.tol, lim.tol_pct, lim.min, lim.max)):
            raise LimitError("nominal needs a tolerance (tol or tol_pct) or min/max; "
                             "a bare nominal would pass every value")
        if lim.tol is not None and lim.tol < 0 or lim.tol_pct is not None and lim.tol_pct < 0:
            raise LimitError("tolerances cannot be negative")
        if lim.min is not None and lim.max is not None and lim.min > lim.max:
            raise LimitError(f"min ({lim.min:g}) is greater than max ({lim.max:g})")
        return lim

    # ----------------------------------------------------------------- bounds
    def bounds(self) -> tuple[Optional[float], Optional[float]]:
        """Effective numeric (low, high) bounds after applying tolerances."""
        lo, hi = self.min, self.max
        if self.nominal is not None:
            if self.tol is not None:
                lo = self.nominal - self.tol if lo is None else max(lo, self.nominal - self.tol)
                hi = self.nominal + self.tol if hi is None else min(hi, self.nominal + self.tol)
            if self.tol_pct is not None:
                d = abs(self.nominal) * self.tol_pct / 100.0
                lo = self.nominal - d if lo is None else max(lo, self.nominal - d)
                hi = self.nominal + d if hi is None else min(hi, self.nominal + d)
        return lo, hi

    def is_empty(self) -> bool:
        return all(v is None for v in (self.min, self.max, self.nominal, self.tol,
                                       self.tol_pct, self.equals, self.one_of, self.regex))

    # --------------------------------------------------------------- describe
    def describe(self, units: str = "") -> str:
        parts: list[str] = []
        if self.nominal is not None and (self.tol is not None or self.tol_pct is not None):
            tol_txt = []
            if self.tol is not None:
                tol_txt.append(f"±{_fmt(self.tol, units)}")
            if self.tol_pct is not None:
                tol_txt.append(f"±{_fmt(self.tol_pct)}%")
            parts.append(f"{_fmt(self.nominal, units)} {' '.join(tol_txt)}")
        elif self.nominal is not None:
            parts.append(f"nominal {_fmt(self.nominal, units)}")
        if self.min is not None and self.max is not None:
            parts.append(f"{_fmt(self.min)} – {_fmt(self.max, units)}")
        elif self.min is not None:
            parts.append(f"≥ {_fmt(self.min, units)}")
        elif self.max is not None:
            parts.append(f"≤ {_fmt(self.max, units)}")
        if self.equals is not None:
            parts.append(f"= {_fmt(self.equals)}")
        if self.one_of is not None:
            parts.append("one of [" + ", ".join(_fmt(o) for o in self.one_of) + "]")
        if self.regex is not None:
            parts.append(f"matches /{self.regex}/")
        return "; ".join(parts) if parts else "(none)"

    # ------------------------------------------------------------------ check
    def check(self, value: Any) -> tuple[bool, str]:
        """Return (passed, reason)."""
        if self.is_empty():
            return True, "no limits"
        reasons: list[str] = []
        ok = True

        numeric_needed = any(v is not None for v in (self.min, self.max, self.nominal))
        if numeric_needed:
            try:
                x = float(value)
            except (TypeError, ValueError):
                return False, f"value {value!r} is not numeric"
            if x != x:  # NaN
                return False, "value is NaN"
            lo, hi = self.bounds()
            if lo is not None and x < lo:
                ok = False
                reasons.append(f"{_fmt(x)} < {_fmt(lo)} (low limit)")
            if hi is not None and x > hi:
                ok = False
                reasons.append(f"{_fmt(x)} > {_fmt(hi)} (high limit)")

        if self.equals is not None:
            if not _equal(value, self.equals):
                ok = False
                reasons.append(f"{value!r} != {self.equals!r}")

        if self.one_of is not None:
            if not any(_equal(value, o) for o in self.one_of):
                ok = False
                reasons.append(f"{value!r} not in {self.one_of!r}")

        if self.regex is not None:
            if re.search(self.regex, str(value)) is None:
                ok = False
                reasons.append(f"{str(value)!r} does not match /{self.regex}/")

        return ok, ("within limits" if ok else "; ".join(reasons))


_TRUE_WORDS = {"1", "ON", "TRUE", "YES"}
_FALSE_WORDS = {"0", "OFF", "FALSE", "NO"}


def _norm(v: Any) -> Any:
    """Normalise booleans and their SCPI spellings so ON == True == 1 and OFF == False == 0."""
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().strip('"')
    u = s.upper()
    if u in _TRUE_WORDS:
        return 1.0
    if u in _FALSE_WORDS:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return u


def _equal(a: Any, b: Any) -> bool:
    """Loose equality: numbers compare numerically, strings case-insensitively,
    ON/OFF/TRUE/FALSE/1/0 are interchangeable."""
    na, nb = _norm(a), _norm(b)
    if isinstance(na, float) and isinstance(nb, float):
        return na == nb
    return str(na) == str(nb)
