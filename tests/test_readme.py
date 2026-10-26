"""Keeps the README's example plan honest: it must parse, validate and run."""
import re
from pathlib import Path

import yaml

from benchplan import plan_from_dict, run_plan

README = Path(__file__).resolve().parents[1] / "README.md"


def _readme_plan() -> dict:
    text = README.read_text(encoding="utf-8")
    blocks = re.findall(r"```yaml\n(.*?)```", text, re.S)
    plan_block = next(b for b in blocks if b.startswith("name:"))
    return yaml.safe_load(plan_block)


def test_readme_plan_parses_and_runs_with_simulated_instruments():
    raw = _readme_plan()
    # swap the real VISA resources for simulated ones that answer every query used in the plan
    sim = {"state_group": "bench", "seed": 1,
           "state": {"v_set": 0.0, "out": 0, "i_load": 0.0},
           "derived": {"v_out": "(v_set - 0.01 * i_load) if out else 0.0"},
           "commands": {
               r"APPL CH1,(?P<v>[-\d.]+),(?P<i>[-\d.]+)": {"set": {"v_set": "v"}},
               r"OUTP CH1,(?P<s>ON|OFF)": {"set": {"out": "1 if s == 'ON' else 0"}},
               r"CURR:LOAD (?P<i>[-\d.]+)": {"set": {"i_load": "i"}},
               r"\*IDN\?": "RIGOL TECHNOLOGIES,DP712,0,1",
               r"MEAS:VOLT:DC\?.*": {"response": "v_out + gauss(0, 0.0005)"},
               r"MEAS:VOLT:AC\?": {"response": "0.002"},
               r"SYST:SN\?": {"text": "{__alias__}-0001"},
           }}
    for alias in raw["instruments"]:
        raw["instruments"][alias] = {"resource": f"sim://{alias}", "sim": dict(sim)}
    plan = plan_from_dict(raw)
    result = run_plan(plan, serial="psu-0001")
    assert result.status == "PASS", [(s.name, s.status, s.message) for s in result.steps if s.status != "PASS"]
    assert all(s.status != "ERROR" for s in result.steps)
    assert result.measurements["v_fl"] < result.measurements["v_nl"]

    # the README also promises skip_if works: flip the variable and the step must be skipped
    skipped = run_plan(plan, serial="psu-0001", variables={"quick": True})
    assert any(s.status == "SKIP" and s.name == "Skipped on quick runs" for s in skipped.steps)
