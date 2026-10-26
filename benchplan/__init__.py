"""benchplan – YAML test plans for SCPI instruments: run, judge against limits, report.

Quick start::

    from benchplan import load_plan, run_plan, write_html
    plan = load_plan("psu_regulation.yaml")
    result = run_plan(plan, serial="SN001", operator="seb")
    write_html(result, "report.html")
    print(result.status)   # PASS / FAIL / ERROR
"""
__version__ = "0.1.0"

from .plan import Plan, Step, InstrumentSpec, PlanError, load_plan, plan_from_dict, validate_plan  # noqa: E402
from .limits import Limits, LimitError  # noqa: E402
from .runner import Runner, RunResult, StepResult, run_plan  # noqa: E402
from .report import write_html, write_json, append_csv, write_all, render_html  # noqa: E402
from .instruments import (SimInstrument, VisaInstrument, BaseInstrument, InstrumentError,  # noqa: E402
                          open_instrument, list_resources, reset_sim_groups)
from .safe_eval import safe_eval, ExprError  # noqa: E402

__all__ = [
    "__version__", "Plan", "Step", "InstrumentSpec", "PlanError", "load_plan", "plan_from_dict",
    "validate_plan", "Limits", "LimitError", "Runner", "RunResult", "StepResult", "run_plan",
    "write_html", "write_json", "append_csv", "write_all", "render_html", "SimInstrument",
    "VisaInstrument", "BaseInstrument", "InstrumentError", "open_instrument", "list_resources",
    "reset_sim_groups", "safe_eval", "ExprError",
]
