import json
from pathlib import Path

from benchplan.cli import main

EXAMPLE = Path(__file__).resolve().parents[1] / "benchplan" / "examples" / "psu_regulation.yaml"


def test_demo_exit_codes_and_outputs(tmp_path, capsys):
    rc = main(["demo", "--serial", "SN1", "--operator", "seb", "--out", str(tmp_path), "--seed", "3"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "RESULT: PASS" in out and "[PASS]" in out and "report:" in out
    assert list(tmp_path.glob("*.html")) and list(tmp_path.glob("*.json")) and (tmp_path / "results.csv").exists()

    rc = main(["demo", "--var", "i_lim=2.0", "--out", str(tmp_path), "--quiet"])
    assert rc == 1


def test_run_json_and_validate(tmp_path, capsys):
    rc = main(["run", str(EXAMPLE), "--out", str(tmp_path), "--quiet", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "PASS"

    assert main(["validate", str(EXAMPLE)]) == 0
    assert "OK" in capsys.readouterr().out

    bad = tmp_path / "bad.yaml"
    bad.write_text("name: x\nsteps:\n  - {name: a, query: 'A?'}\n")
    assert main(["validate", str(bad)]) == 3
    assert main(["run", str(bad), "--out", str(tmp_path)]) == 3


def test_example_command_prints_plan(capsys):
    assert main(["example"]) == 0
    assert "instruments:" in capsys.readouterr().out
