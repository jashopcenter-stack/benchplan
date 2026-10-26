"""Result writers: console lines, JSON, self-contained HTML, CSV log."""
from __future__ import annotations

import csv
import html
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .runner import RunResult, StepResult

__all__ = ["console_line", "write_json", "write_html", "append_csv", "write_all", "value_text"]

_ICON = {"PASS": "PASS", "FAIL": "FAIL", "ERROR": "ERR ", "SKIP": "SKIP", "INFO": "INFO", "DONE": " ok "}


def value_text(sr: StepResult) -> str:
    v = sr.value
    if v is None:
        return ""
    if isinstance(v, bool):
        s = str(v)
    elif isinstance(v, float):
        s = f"{v:.6g}"
    else:
        s = str(v)
    return f"{s} {sr.units}".strip() if sr.units else s


def console_line(sr: StepResult, width: int = 44) -> str:
    name = sr.name if len(sr.name) <= width else sr.name[: width - 1] + "…"
    cols = [f"[{_ICON.get(sr.status, sr.status)}]", f"{name:<{width}}"]
    vt = value_text(sr)
    if vt:
        cols.append(f"{vt:>16}")
    if sr.limits:
        cols.append(f"({sr.limits})")
    if sr.status in ("FAIL", "ERROR") and sr.message:
        cols.append(f"-- {sr.message}")
    elif sr.status == "SKIP" and sr.message:
        cols.append(f"-- {sr.message}")
    return "  ".join(cols)


def summary_lines(result: RunResult) -> list[str]:
    c = result.counts
    lines = [
        "-" * 72,
        f"{result.plan_name}  |  S/N: {result.serial or '-'}  |  operator: {result.operator or '-'}",
        f"RESULT: {result.status}   pass={c['PASS']} fail={c['FAIL']} error={c['ERROR']} "
        f"skip={c['SKIP']}   ({result.duration_s:.2f} s)",
    ]
    if result.error:
        lines.append(f"run error: {result.error}")
    return lines


# -------------------------------------------------------------------- JSON

def write_json(result: RunResult, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.to_dict(), indent=2, default=str), encoding="utf-8")
    return path


# --------------------------------------------------------------------- CSV

CSV_FIELDS = ["run_id", "timestamp", "plan", "plan_version", "serial", "operator", "run_status",
              "step_index", "phase", "step", "kind", "instrument", "command", "status",
              "value", "units", "limits", "message", "attempts", "duration_ms"]


def append_csv(result: RunResult, path: str | Path) -> Path:
    """Append one row per step to a long-format CSV (created with header if missing)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        if new:
            w.writeheader()
        for s in result.steps:
            w.writerow({
                "run_id": result.run_id, "timestamp": s.timestamp, "plan": result.plan_name,
                "plan_version": result.plan_version, "serial": result.serial,
                "operator": result.operator, "run_status": result.status,
                "step_index": s.index, "phase": s.phase, "step": s.name, "kind": s.kind,
                "instrument": s.instrument, "command": s.command, "status": s.status,
                "value": "" if s.value is None else s.value, "units": s.units,
                "limits": s.limits, "message": s.message, "attempts": s.attempts,
                "duration_ms": s.duration_ms,
            })
    return path


# -------------------------------------------------------------------- HTML

_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1c2330;--muted:#5f6b7a;--line:#e3e7ec;
--pass:#1b7f4b;--pass-bg:#e4f5ec;--fail:#b3261e;--fail-bg:#fbe9e7;--err:#8a4b00;--err-bg:#fff1dc;
--skip:#5f6b7a;--skip-bg:#eef0f3;--info:#1f5fa8;--info-bg:#e7f0fb;--done:#3d4654;--done-bg:#f1f3f5}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.45 -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:24px 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px 20px;margin-bottom:16px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:0 0 10px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.head{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap}
.badge{font-weight:700;font-size:22px;padding:8px 18px;border-radius:8px;letter-spacing:.06em}
.badge.PASS{background:var(--pass-bg);color:var(--pass)}.badge.FAIL{background:var(--fail-bg);color:var(--fail)}
.badge.ERROR{background:var(--err-bg);color:var(--err)}
.meta{display:flex;flex-wrap:wrap;gap:8px 28px;margin-top:12px}.meta div{min-width:150px}
.meta div span{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.05em}
.counts{display:flex;gap:10px;flex-wrap:wrap;margin-top:12px}
.pill{padding:4px 10px;border-radius:999px;font-weight:600;font-size:12px}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:7px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.05em}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}td.txt{text-align:right;word-break:break-all;font-size:12px}
td.lim{white-space:nowrap}th.step{width:24%}th.cmd{width:22%}th.meas{width:13%}th.lim{width:15%}
.st{display:inline-block;min-width:52px;text-align:center;padding:2px 7px;border-radius:5px;font-weight:700;font-size:11px}
.st.PASS{background:var(--pass-bg);color:var(--pass)}.st.FAIL{background:var(--fail-bg);color:var(--fail)}
.st.ERROR{background:var(--err-bg);color:var(--err)}.st.SKIP{background:var(--skip-bg);color:var(--skip)}
.st.INFO{background:var(--info-bg);color:var(--info)}.st.DONE{background:var(--done-bg);color:var(--done)}
tr.FAIL td,tr.ERROR td{background:#fff8f7}tr.phase td{background:#f6f7f9;color:var(--muted);font-weight:600;font-size:12px}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}
.msg{color:var(--muted)}tr.FAIL .msg,tr.ERROR .msg{color:var(--fail)}
.foot{color:var(--muted);font-size:12px;text-align:center;margin-top:8px}
@media print{body{background:#fff}.card{border:none;padding:0;margin-bottom:14px}.wrap{padding:0}}
"""


def _e(x: Any) -> str:
    return html.escape("" if x is None else str(x))


def _fmt_dt(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return iso or "-"


def render_html(result: RunResult) -> str:
    c = result.counts
    rows: list[str] = []
    last_phase = None
    for s in result.steps:
        if s.phase != last_phase:
            rows.append(f'<tr class="phase"><td colspan="7">{_e(s.phase.upper())}</td></tr>')
            last_phase = s.phase
        rows.append(
            f'<tr class="{_e(s.status)}">'
            f'<td class="num">{s.index}</td>'
            f'<td><span class="st {_e(s.status)}">{_e(s.status)}</span></td>'
            f'<td>{_e(s.name)}{"<br><span class=msg>" + _e(s.note) + "</span>" if s.note else ""}</td>'
            f'<td class="mono">{_e(s.instrument)}{(" · " if s.instrument and s.command else "")}{_e(s.command)}</td>'
            f'<td class="{"num" if isinstance(s.value, (int, float)) or s.value is None else "txt"}">{_e(value_text(s))}</td>'
            f'<td class="lim">{_e(s.limits)}</td>'
            f'<td class="msg">{_e(s.message)}{(" · " + str(s.attempts) + " attempts") if s.attempts > 1 else ""}</td>'
            f'</tr>'
        )
    inst_rows = "".join(f"<tr><td class='mono'>{_e(a)}</td><td class='mono'>{_e(i)}</td></tr>"
                        for a, i in result.instruments.items()) or "<tr><td colspan=2>-</td></tr>"
    var_rows = "".join(f"<tr><td class='mono'>{_e(k)}</td><td class='mono'>{_e(v)}</td></tr>"
                       for k, v in result.variables.items() if k not in ("serial", "operator")) or "<tr><td colspan=2>-</td></tr>"
    meas_rows = "".join(f"<tr><td class='mono'>{_e(k)}</td><td class='mono'>{_e(f'{v:.6g}' if isinstance(v, float) else v)}</td></tr>"
                        for k, v in result.measurements.items()) or "<tr><td colspan=2>-</td></tr>"
    err = f'<div class="card" style="border-color:var(--err)"><h2>Run error</h2><div class="mono">{_e(result.error)}</div></div>' if result.error else ""
    pills = " ".join(f'<span class="pill st {k}">{k} {c[k]}</span>' for k in ("PASS", "FAIL", "ERROR", "SKIP") if c[k])
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(result.plan_name)} – {_e(result.serial or 'no S/N')} – {_e(result.status)}</title>
<style>{_CSS}</style></head><body><div class="wrap">
<div class="card"><div class="head"><div>
<h1>{_e(result.plan_name)}{(' <span class="msg">v' + _e(result.plan_version) + '</span>') if result.plan_version else ''}</h1>
<div class="msg">{_e(result.plan_description)}</div></div>
<div class="badge {_e(result.status)}">{_e(result.status)}</div></div>
<div class="meta">
<div><span>Serial number</span>{_e(result.serial or '-')}</div>
<div><span>Operator</span>{_e(result.operator or '-')}</div>
<div><span>Started</span>{_e(_fmt_dt(result.started_at))}</div>
<div><span>Duration</span>{result.duration_s:.2f} s</div>
<div><span>Run ID</span><span class="mono" style="display:inline;color:inherit;font-size:12px;text-transform:none;letter-spacing:0">{_e(result.run_id)}</span></div>
<div><span>Plan file</span>{_e(Path(result.plan_source).name if result.plan_source else '-')}</div>
</div><div class="counts">{pills}</div></div>
{err}
<div class="card"><h2>Results</h2><table>
<thead><tr><th>#</th><th>Status</th><th class="step">Step</th><th class="cmd">Instrument · command</th><th class="meas" style="text-align:right">Measured</th><th class="lim">Limits</th><th>Notes</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>
<div class="card" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:20px">
<div><h2>Instruments</h2><table><tbody>{inst_rows}</tbody></table></div>
<div><h2>Variables</h2><table><tbody>{var_rows}</tbody></table></div>
<div><h2>Saved measurements</h2><table><tbody>{meas_rows}</tbody></table></div>
</div>
<div class="foot">Generated by benchplan · {_e(_fmt_dt(result.finished_at))}</div>
</div></body></html>"""


def write_html(result: RunResult, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_html(result), encoding="utf-8")
    return path


# ------------------------------------------------------------------- bundle

def write_all(result: RunResult, out_dir: str | Path, plan_slug: str, csv_name: str = "results.csv") -> dict[str, Path]:
    out = Path(out_dir)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    sn = (result.serial or "nosn").replace("/", "-").replace(" ", "_")
    base = f"{plan_slug}_{sn}_{stamp}"
    return {
        "html": write_html(result, out / f"{base}.html"),
        "json": write_json(result, out / f"{base}.json"),
        "csv": append_csv(result, out / csv_name),
    }
