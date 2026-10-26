from pathlib import Path

import pytest
import yaml

from benchplan.plan import PlanError, load_plan, plan_from_dict, validate_plan

EXAMPLE = Path(__file__).resolve().parents[1] / "benchplan" / "examples" / "psu_regulation.yaml"


def test_example_loads():
    plan = load_plan(EXAMPLE)
    assert plan.name.startswith("DC Power Supply")
    assert set(plan.instruments) == {"psu", "dmm", "load"}
    assert plan.instruments["psu"].is_sim
    assert plan.variables["v_set"] == 5.0
    assert plan.setup and plan.steps and plan.teardown
    assert plan.slug == "dc-power-supply-acceptance-test"
    assert validate_plan(EXAMPLE) == []


def _minimal(**over):
    raw = {
        "name": "t",
        "instruments": {"psu": "sim://psu"},
        "steps": [{"name": "v", "instrument": "psu", "query": "VOLT?", "limits": {"max": 1}}],
    }
    raw.update(over)
    return raw


def test_minimal_plan_and_string_instrument():
    plan = plan_from_dict(_minimal())
    assert plan.instruments["psu"].resource == "sim://psu"
    assert plan.steps[0].kind == "query"
    assert plan.steps[0].has_limits


def test_auto_named_steps():
    plan = plan_from_dict(_minimal(setup=[{"instrument": "psu", "write": "*RST"}, {"delay_ms": 10}]))
    assert plan.setup[0].name == "write *RST"
    assert plan.setup[1].name == "delay 10 ms"
    assert plan.setup[1].kind == "delay"


@pytest.mark.parametrize("raw, msg", [
    ({"name": "x"}, "no 'steps'"),
    ({"steps": []}, "needs a 'name'"),
    (_minimal(steps=[{"name": "a", "instrument": "psu"}]), "exactly one of"),
    (_minimal(steps=[{"name": "a", "instrument": "psu", "query": "A?", "write": "B"}]), "exactly one of"),
    (_minimal(steps=[{"name": "a", "instrument": "nope", "query": "A?"}]), "unknown instrument"),
    (_minimal(steps=[{"name": "a", "query": "A?"}]), "need an 'instrument'"),
    (_minimal(steps=[{"name": "a", "instrument": "psu", "write": "X", "limits": {"max": 1}}]), "cannot have limits"),
    (_minimal(steps=[{"name": "a", "instrument": "psu", "query": "A?", "limits": {"maxx": 1}}]), "unknown limit key"),
    (_minimal(steps=[{"name": "a", "instrument": "psu", "query": "A?", "parse": "hex"}]), "parse must be"),
    (_minimal(steps=[{"name": "a", "instrument": "psu", "query": "A?", "save_as": "bad name"}]), "save_as"),
    (_minimal(steps=[{"name": "a", "instrument": "psu", "query": "A?", "bogus": 1}]), "unknown key"),
    (_minimal(steps=["*RST"]), "must be a mapping"),
    (_minimal(on_fail="explode"), "on_fail must be"),
    (_minimal(instruments={"psu": {"timeout_ms": 1}}), "needs a 'resource'"),
    (_minimal(instruments={"bad alias": "sim://x"}), "valid identifier"),
    (_minimal(variables={"9x": 1}), "valid identifier"),
])
def test_validation_errors(raw, msg):
    with pytest.raises(PlanError) as exc:
        plan_from_dict(raw)
    assert msg in str(exc.value)


def test_validate_reports_file_problems(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: x\nsteps: [{name: a, query: 'A?'}]\n")
    problems = validate_plan(bad)
    assert problems and "instrument" in problems[0]
    missing = validate_plan(tmp_path / "nope.yaml")
    assert missing and "cannot read" in missing[0]


def test_termination_unescape():
    plan = plan_from_dict(_minimal(instruments={"psu": {"resource": "sim://p", "read_termination": "\\r\\n"}}))
    assert plan.instruments["psu"].read_termination == "\r\n"


def test_example_is_valid_yaml_for_docs():
    raw = yaml.safe_load(EXAMPLE.read_text())
    assert raw["on_fail"] == "continue"
