import pytest

from benchplan.safe_eval import ExprError, safe_eval


def test_arithmetic_and_names():
    assert safe_eval("(a + b) * 2 / 4", {"a": 1, "b": 3}) == 2.0
    assert safe_eval("-x ** 2", {"x": 3}) == -9
    assert safe_eval("7 % 3 + 7 // 3") == 3


def test_functions_and_constants():
    assert safe_eval("abs(-2.5)") == 2.5
    assert safe_eval("round(sqrt(16) + pi, 2)") == 7.14
    assert safe_eval("max(1, 2, 3) - min(1, 2, 3)") == 2
    assert safe_eval("mean([1, 2, 3])") == 2


def test_comparisons_and_logic():
    assert safe_eval("1 < x <= 3", {"x": 3}) is True
    assert safe_eval("x > 10 or y == 'ON'", {"x": 0, "y": "ON"}) is True
    assert safe_eval("not (x and y)", {"x": 1, "y": 0}) is True
    assert safe_eval("'CC' if cc else 'CV'", {"cc": 0}) == "CV"


def test_numbers_pass_through():
    assert safe_eval(5) == 5
    assert safe_eval(2.5) == 2.5
    assert safe_eval(True) is True


@pytest.mark.parametrize("bad", [
    "__import__('os')",
    "(1).__class__",
    "lambda: 1",
    "[x for x in range(3)]",
    "open('/etc/passwd')",
    "x.real",
    "unknown_name + 1",
    "",
    "1 +",
])
def test_rejects_unsafe_or_invalid(bad):
    with pytest.raises(ExprError):
        safe_eval(bad, {"x": 1})
