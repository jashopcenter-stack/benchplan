from pathlib import Path

import pytest

from benchplan import (InstrumentError, Runner, load_plan, plan_from_dict, reset_sim_groups, run_plan)
from benchplan.plan import InstrumentSpec

EXAMPLE = Path(__file__).resolve().parents[1] / "benchplan" / "examples" / "psu_regulation.yaml"


@pytest.fixture(autouse=True)
def _fresh_sim():
    reset_sim_groups()
    yield
    reset_sim_groups()


def test_demo_passes():
    result = run_plan(load_plan(EXAMPLE), serial="SN-001", operator="tester")
    assert result.status == "PASS", [(s.name, s.status, s.message) for s in result.steps if s.status != "PASS"]
    assert result.counts["FAIL"] == 0 and result.counts["ERROR"] == 0
    assert result.serial == "SN-001" and result.operator == "tester"
    assert "DP712" in result.instruments["psu"]
    assert 4.9 < result.measurements["v_nl"] < 5.1
    assert result.measurements["v_fl"] < result.measurements["v_nl"]
    phases = {s.phase for s in result.steps}
    assert phases == {"setup", "test", "teardown"}
    assert result.duration_s >= 0
    # derived step evaluated and judged
    reg = next(s for s in result.steps if s.name == "Load regulation")
    assert reg.kind == "expr" and reg.status == "PASS" and 0 <= reg.value < 0.5


def test_demo_fails_when_current_limit_never_engages():
    result = run_plan(load_plan(EXAMPLE), variables={"i_lim": 2.0})
    assert result.status == "FAIL"
    failed = {s.name for s in result.steps if s.status == "FAIL"}
    assert {"Current limit engages", "PSU reports CC mode", "Voltage folds back in CC"} <= failed


def test_abort_on_fail_skips_rest_but_runs_teardown():
    result = run_plan(load_plan(EXAMPLE), variables={"v_set": 9.0, "i_lim": 2.0}, on_fail="abort")
    # "Voltage folds back in CC" uses v_set*0.8, so the first FAIL is "Current limit engages"
    assert result.status == "FAIL" and result.aborted
    names = [s.name for s in result.steps]
    i_fail = names.index("Current limit engages")
    after = result.steps[i_fail + 1:]
    assert all(s.status == "SKIP" for s in after if s.phase == "test")
    assert all(s.status == "DONE" for s in after if s.phase == "teardown")


def _plan(steps, **extra):
    raw = {
        "name": "unit",
        "instruments": {
            "psu": {"resource": "sim://psu", "sim": {
                "seed": 1,
                "state": {"v": 1.0, "n": 0},
                "commands": {
                    r"V\?": {"response": "v"},
                    r"FLAKY\?": {"set": {"n": "n + 1"}, "response": "1.0 if n >= 2 else 9.0"},
                    r"TXT\?": "hello {v}",
                    r"BOOL\?": "ON",
                    r"V (?P<x>[-\d.]+)": {"set": {"v": "x"}},
                },
            }},
        },
        "steps": steps,
    }
    raw.update(extra)
    return plan_from_dict(raw)


def test_parse_modes_scale_and_save_as():
    plan = _plan([
        {"name": "f", "instrument": "psu", "query": "V?", "scale": 1000, "units": "mV", "save_as": "mv"},
        {"name": "s", "instrument": "psu", "query": "TXT?", "parse": "str", "save_as": "txt"},
        {"name": "b", "instrument": "psu", "query": "BOOL?", "parse": "bool", "limits": {"equals": True}},
        {"name": "i", "instrument": "psu", "query": "V?", "parse": "int", "scale": 3, "limits": {"equals": 3}},
        {"name": "e", "expr": "mv / 1000 + 1", "limits": {"nominal": 2, "tol": 0.001}},
    ])
    r = run_plan(plan)
    assert r.status == "PASS"
    assert r.measurements["mv"] == 1000.0
    assert r.measurements["txt"] == "hello 1.0"


def test_placeholders_in_commands_and_limits():
    plan = _plan([
        {"name": "set", "instrument": "psu", "write": "V {target}"},
        {"name": "rb", "instrument": "psu", "query": "V?", "limits": {"nominal": "{target}", "tol": "target*0.01"}},
        {"name": "rb2", "instrument": "psu", "query": "V?", "limits": {"max": "target + 0.5", "min": "target - 0.5"}},
    ], variables={"target": 3.3})
    r = run_plan(plan)
    assert r.status == "PASS", [(s.name, s.message) for s in r.steps]
    assert r.steps[0].command == "V 3.3"


def test_retries_recover_flaky_measurement():
    plan = _plan([{"name": "flaky", "instrument": "psu", "query": "FLAKY?", "retries": 2,
                   "retry_delay_ms": 1, "limits": {"max": 2.0}}])
    r = run_plan(plan)
    assert r.status == "PASS"
    assert r.steps[0].attempts == 2
    assert "attempt 2" in r.steps[0].message


def test_skip_if_and_info_steps():
    plan = _plan([
        {"name": "skipped", "instrument": "psu", "query": "V?", "skip_if": "fast", "limits": {"max": 0}},
        {"name": "info only", "instrument": "psu", "query": "V?"},
    ], variables={"fast": True})
    r = run_plan(plan)
    assert r.status == "PASS"
    assert r.steps[0].status == "SKIP" and r.steps[1].status == "INFO"


def test_unknown_query_is_error_and_unknown_placeholder_is_error():
    plan = _plan([
        {"name": "bad cmd", "instrument": "psu", "query": "NOPE?", "limits": {"max": 1}},
        {"name": "bad ph", "instrument": "psu", "query": "V? {missing}", "limits": {"max": 1}},
        {"name": "bad expr", "expr": "1 / 'a'", "limits": {"max": 1}},
    ])
    r = run_plan(plan)
    assert r.status == "ERROR"
    assert [s.status for s in r.steps] == ["ERROR", "ERROR", "ERROR"]
    assert "no response" in r.steps[0].message
    assert "unknown placeholder" in r.steps[1].message


def test_instrument_open_failure_is_run_error():
    plan = _plan([{"name": "v", "instrument": "psu", "query": "V?"}])

    def boom(spec: InstrumentSpec):
        raise InstrumentError("cannot open")

    r = Runner(plan, open_fn=boom).run()
    assert r.status == "ERROR" and "cannot open" in r.error


def test_visa_backend_missing_or_bad_resource():
    plan = plan_from_dict({"name": "v", "instruments": {"x": "GPIB0::99::INSTR"},
                           "steps": [{"name": "q", "instrument": "x", "query": "*IDN?"}]})
    r = run_plan(plan)
    assert r.status == "ERROR"
    assert r.error  # either pyvisa missing or resource cannot be opened


def test_pyvisa_sim_backend_roundtrip():
    pytest.importorskip("pyvisa")
    pytest.importorskip("pyvisa_sim")
    # pyvisa-sim ships a mock SCPI device on GPIB::9 (see pyvisa_sim/default.yaml)
    plan = plan_from_dict({
        "name": "visa-sim",
        "instruments": {"dev": {"resource": "GPIB0::9::INSTR", "visa_library": "@sim",
                                "read_termination": "\\n", "write_termination": "\\n"}},
        "setup": [{"instrument": "dev", "write": ":VOLT:IMM:AMPL 2.5"}],
        "steps": [
            {"name": "idn", "instrument": "dev", "query": "*IDN?", "parse": "str", "limits": {"regex": "MOCK"}},
            {"name": "volt", "instrument": "dev", "query": ":VOLT:IMM:AMPL?", "units": "V",
             "limits": {"nominal": 2.5, "tol": 0.001}},
        ],
    })
    r = run_plan(plan)
    assert r.status == "PASS", [(s.name, s.status, s.message) for s in r.steps]
    assert "SCPI,MOCK" in r.instruments["dev"]
