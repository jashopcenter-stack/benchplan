import pytest

from benchplan.limits import LimitError, Limits


def test_min_max():
    lim = Limits.from_spec({"min": 4.95, "max": 5.05})
    assert lim.check(5.0) == (True, "within limits")
    ok, why = lim.check(5.1)
    assert not ok and "high limit" in why
    ok, why = lim.check(4.9)
    assert not ok and "low limit" in why
    assert lim.describe("V") == "4.95 – 5.05 V"


def test_nominal_abs_and_pct_tolerance():
    lim = Limits.from_spec({"nominal": 5.0, "tol": 0.05})
    assert lim.bounds() == (4.95, 5.05)
    assert lim.check(5.05)[0] and not lim.check(5.0501)[0]
    assert "±0.05 V" in lim.describe("V")

    pct = Limits.from_spec({"nominal": 100, "tol_pct": 1})
    assert pct.bounds() == (99.0, 101.0)
    assert pct.check(99)[0] and not pct.check(98.9)[0]
    assert "±1%" in pct.describe()


def test_combined_bounds_take_tightest():
    lim = Limits.from_spec({"nominal": 5.0, "tol": 0.5, "max": 5.2})
    assert lim.bounds() == (4.5, 5.2)


def test_equals_one_of_regex():
    assert Limits.from_spec({"equals": "ON"}).check("on")[0]
    assert Limits.from_spec({"equals": 1}).check("1.0")[0]
    assert not Limits.from_spec({"equals": "CC"}).check("CV")[0]
    assert Limits.from_spec({"one_of": ["CV", "CC"]}).check("CC")[0]
    assert not Limits.from_spec({"one_of": ["CV", "CC"]}).check("UR")[0]
    assert Limits.from_spec({"regex": "^RIGOL"}).check("RIGOL TECHNOLOGIES,DP712")[0]
    assert not Limits.from_spec({"regex": "^RIGOL"}).check("Keysight")[0]


def test_non_numeric_value_fails_numeric_limits():
    ok, why = Limits.from_spec({"min": 0}).check("abc")
    assert not ok and "not numeric" in why
    ok, why = Limits.from_spec({"min": 0}).check(float("nan"))
    assert not ok and "NaN" in why


def test_resolver_is_used():
    env = {"v_set": 5.0}
    lim = Limits.from_spec({"nominal": "{v_set}", "tol": "0.1"},
                           resolve=lambda v: float(str(v).format(**env)))
    assert lim.bounds() == (4.9, 5.1)


@pytest.mark.parametrize("spec", [
    {"tol": 0.1},                 # tol without nominal
    {"min": 5, "max": 4},         # inverted
    {"bogus": 1},                 # unknown key
    {"min": "abc"},               # not a number
    {"regex": "("},               # invalid regex
    {"one_of": "CC"},             # not a list
])
def test_malformed(spec):
    with pytest.raises(LimitError):
        Limits.from_spec(spec)


def test_empty_limits_pass():
    assert Limits().check("anything") == (True, "no limits")
