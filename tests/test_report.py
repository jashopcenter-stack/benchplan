import csv
import json
from pathlib import Path

import pytest

from benchplan import load_plan, reset_sim_groups, run_plan, write_all
from benchplan.report import console_line, render_html, summary_lines

EXAMPLE = Path(__file__).resolve().parents[1] / "benchplan" / "examples" / "psu_regulation.yaml"


@pytest.fixture
def result():
    reset_sim_groups()
    return run_plan(load_plan(EXAMPLE), serial="SN 7/A", operator="seb")


def test_write_all_produces_html_json_csv(tmp_path, result):
    paths = write_all(result, tmp_path, "psu")
    assert paths["html"].exists() and paths["json"].exists() and paths["csv"].exists()
    assert "SN-7-A" in paths["html"].name and result.run_id[:6] in paths["html"].name  # sanitised + unique

    html = paths["html"].read_text(encoding="utf-8")
    assert "DC Power Supply Acceptance Test" in html
    assert 'class="badge PASS"' in html
    assert "Load regulation" in html and "±0.005 V" in html
    assert "<script" not in html  # self-contained, no scripts needed

    data = json.loads(paths["json"].read_text(encoding="utf-8"))
    assert data["status"] == "PASS" and data["serial"] == "SN 7/A"
    assert data["counts"]["PASS"] == sum(1 for s in data["steps"] if s["status"] == "PASS")
    assert data["measurements"]["v_nl"] > 4.9

    rows = list(csv.DictReader(paths["csv"].open(encoding="utf-8-sig")))
    assert len(rows) == len(result.steps)
    assert rows[0]["run_id"] == result.run_id and rows[0]["plan"] == result.plan_name

    # a second run appends, keeping one header
    reset_sim_groups()
    write_all(run_plan(load_plan(EXAMPLE), serial="SN2"), tmp_path, "psu")
    rows2 = list(csv.DictReader(paths["csv"].open(encoding="utf-8-sig")))
    assert len(rows2) == 2 * len(result.steps)
    assert {r["serial"] for r in rows2} == {"SN 7/A", "SN2"}


def test_html_escapes_and_shows_failures(result):
    result.steps[2].status = "FAIL"
    result.steps[2].message = "<script>alert(1)</script>"
    result.status = "FAIL"
    html = render_html(result)
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert 'class="badge FAIL"' in html


def test_console_and_summary_lines(result):
    lines = [console_line(s) for s in result.steps]
    assert any(line.startswith("[PASS]") and "V" in line for line in lines)
    summary = "\n".join(summary_lines(result))
    assert "RESULT: PASS" in summary and "SN 7/A" in summary


def test_json_never_contains_nan(tmp_path, result):
    result.steps[2].value = float("nan")
    result.measurements["bad"] = float("inf")
    from benchplan import write_json
    path = write_json(result, tmp_path / "r.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["steps"][2]["value"] is None and data["measurements"]["bad"] is None


def test_file_names_are_filesystem_safe(tmp_path):
    reset_sim_groups()
    r = run_plan(load_plan(EXAMPLE), serial='A:B*C?"<>|\\D')
    paths = write_all(r, tmp_path, "x")
    assert paths["html"].exists() and "A-B-C-D" in paths["html"].name
