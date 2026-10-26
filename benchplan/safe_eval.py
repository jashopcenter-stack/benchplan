"""Small, safe expression evaluator.

Used for derived measurements (``expr`` steps), numeric limit values that
reference variables, ``skip_if`` conditions and simulated-instrument
responses.

Only arithmetic, comparisons, boolean logic, a fixed set of math/random
functions and plain names are allowed. No attribute access, no imports,
no lambdas, no comprehensions, no dunder tricks. Resource use is bounded:
expression length, exponent size, integer size and sequence repetition are
all capped, so a hostile plan file can at worst produce an ERROR step.
"""
from __future__ import annotations

import ast
import math
import operator
import random
from typing import Any, Mapping

__all__ = ["safe_eval", "ExprError", "DEFAULT_FUNCS", "default_funcs"]

MAX_EXPR_LEN = 4000
MAX_EXPONENT = 1000
MAX_INT_BITS = 4096
MAX_REPEAT = 100_000


class ExprError(ValueError):
    """Raised when an expression is invalid, uses something not allowed, or cannot be evaluated."""


def _mean(xs):
    xs = list(xs)
    if not xs:
        raise ExprError("mean() of empty sequence")
    return sum(xs) / len(xs)


def default_funcs(rng: random.Random | None = None) -> dict[str, Any]:
    """Return the function/constant table, optionally bound to a seeded RNG."""
    rng = rng or random
    return {
        # builtins
        "abs": abs, "min": min, "max": max, "round": round,
        "float": float, "int": int, "str": str, "len": len, "sum": sum,
        "bool": bool,
        # math
        "sqrt": math.sqrt, "log": math.log, "log10": math.log10, "exp": math.exp,
        "pow": math.pow, "floor": math.floor, "ceil": math.ceil,
        "sin": math.sin, "cos": math.cos, "tan": math.tan, "atan2": math.atan2,
        "hypot": math.hypot, "mean": _mean,
        # random (for simulated instruments)
        "gauss": rng.gauss, "uniform": rng.uniform, "random": rng.random,
        # constants
        "pi": math.pi, "e": math.e, "inf": math.inf,
        "True": True, "False": False, "None": None,
    }


DEFAULT_FUNCS = default_funcs()


def _checked_int(v: Any) -> Any:
    if isinstance(v, int) and not isinstance(v, bool) and v.bit_length() > MAX_INT_BITS:
        raise ExprError(f"integer result too large (> {MAX_INT_BITS} bits)")
    return v


def _pow(a, b):
    if isinstance(b, (int, float)) and not isinstance(b, bool) and abs(b) > MAX_EXPONENT:
        raise ExprError(f"exponent too large (|exp| > {MAX_EXPONENT})")
    return _checked_int(operator.pow(a, b))


def _mul(a, b):
    for seq, n in ((a, b), (b, a)):
        if isinstance(seq, (str, list, tuple)) and isinstance(n, int) and not isinstance(n, bool):
            if n * len(seq) > MAX_REPEAT:
                raise ExprError(f"sequence repetition too large (> {MAX_REPEAT} elements)")
    return _checked_int(operator.mul(a, b))


def _add(a, b):
    if isinstance(a, (str, list, tuple)) and isinstance(b, (str, list, tuple)) and len(a) + len(b) > MAX_REPEAT:
        raise ExprError(f"sequence too large (> {MAX_REPEAT} elements)")
    return _checked_int(operator.add(a, b))


_BIN = {
    ast.Add: _add, ast.Sub: lambda a, b: _checked_int(operator.sub(a, b)), ast.Mult: _mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: _pow,
}
_UNARY = {ast.USub: operator.neg, ast.UAdd: operator.pos, ast.Not: operator.not_}
_CMP = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
    ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b,
}


class _Evaluator(ast.NodeVisitor):
    def __init__(self, env: Mapping[str, Any]):
        self.env = env

    def generic_visit(self, node):  # anything not explicitly handled is rejected
        raise ExprError(f"'{type(node).__name__}' is not allowed in expressions")

    def visit_Constant(self, node):
        if isinstance(node.value, (int, float, str, bool)) or node.value is None:
            return node.value
        raise ExprError(f"constant of type {type(node.value).__name__} not allowed")

    def visit_Name(self, node):
        if node.id in self.env:
            return self.env[node.id]
        raise ExprError(f"unknown name '{node.id}'")

    def visit_BinOp(self, node):
        op = _BIN.get(type(node.op))
        if op is None:
            raise ExprError(f"operator {type(node.op).__name__} not allowed")
        return op(self.visit(node.left), self.visit(node.right))

    def visit_UnaryOp(self, node):
        op = _UNARY.get(type(node.op))
        if op is None:
            raise ExprError(f"operator {type(node.op).__name__} not allowed")
        return op(self.visit(node.operand))

    def visit_BoolOp(self, node):
        if isinstance(node.op, ast.And):
            result = True
            for v in node.values:
                result = self.visit(v)
                if not result:
                    return result
            return result
        result = False
        for v in node.values:
            result = self.visit(v)
            if result:
                return result
        return result

    def visit_Compare(self, node):
        left = self.visit(node.left)
        for op, comparator in zip(node.ops, node.comparators):
            fn = _CMP.get(type(op))
            if fn is None:
                raise ExprError(f"comparison {type(op).__name__} not allowed")
            right = self.visit(comparator)
            if not fn(left, right):
                return False
            left = right
        return True

    def visit_IfExp(self, node):
        return self.visit(node.body) if self.visit(node.test) else self.visit(node.orelse)

    def visit_Call(self, node):
        if not isinstance(node.func, ast.Name):
            raise ExprError("only plain function calls are allowed")
        fn = self.env.get(node.func.id)
        if not callable(fn):
            raise ExprError(f"'{node.func.id}' is not a callable function")
        if node.keywords:
            raise ExprError("keyword arguments are not allowed")
        return _checked_int(fn(*[self.visit(a) for a in node.args]))

    def visit_List(self, node):
        return [self.visit(e) for e in node.elts]

    def visit_Tuple(self, node):
        return tuple(self.visit(e) for e in node.elts)

    def visit_Subscript(self, node):
        container = self.visit(node.value)
        index = self.visit(node.slice)
        try:
            return container[index]
        except (KeyError, IndexError, TypeError) as exc:
            raise ExprError(f"bad subscript: {exc}") from None


def safe_eval(expr: Any, env: Mapping[str, Any] | None = None,
              funcs: Mapping[str, Any] | None = None) -> Any:
    """Evaluate ``expr`` with names from ``env`` plus the allowed functions.

    Numbers and booleans are returned as-is (no parsing needed).
    """
    if isinstance(expr, (int, float, bool)) or expr is None:
        return expr
    text = str(expr).strip()
    if not text:
        raise ExprError("empty expression")
    if len(text) > MAX_EXPR_LEN:
        raise ExprError(f"expression too long (> {MAX_EXPR_LEN} characters)")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise ExprError(f"invalid expression {text!r}: {exc.msg}") from None
    except (RecursionError, MemoryError, ValueError) as exc:
        raise ExprError(f"expression too complex: {type(exc).__name__}") from None
    scope: dict[str, Any] = dict(funcs if funcs is not None else DEFAULT_FUNCS)
    scope.update(env or {})
    try:
        return _Evaluator(scope).visit(tree.body)
    except ExprError:
        raise
    except (ArithmeticError, TypeError, ValueError, RecursionError, MemoryError) as exc:
        raise ExprError(f"cannot evaluate {text!r}: {type(exc).__name__}: {exc}") from None
