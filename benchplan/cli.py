"""Command-line interface.

    benchplan run PLAN.yaml --serial SN001 --operator seb
    benchplan validate PLAN.yaml
    benchplan demo                     # bundled example, simulated instruments
    benchplan demo --var i_lim=2.0     # make the demo fail on purpose
    benchplan resources                # list VISA resources (needs pyvisa)
"""
from __future__ import annotations

import argparse
import sys
from importlib import resources as ires
from pathlib import Path

from . import __version__
from .instruments import InstrumentError, list_resources, reset_sim_groups
from .plan import PlanError, load_plan, validate_plan
from .report import console_line, summary_lines, write_all
from .runner import Runner

EXIT_PASS, EXIT_FAIL, EXIT_ERROR, EXIT_INVALID = 0, 1, 2, 3


def _parse_vars(items: list[str] | None) -> dict:
    out: dict = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"--var expects NAME=VALUE, got {item!r}")
        k, v = item.split("=", 1)
        k = k.strip()
        v = v.strip()
        try:
            out[k] = int(v) if v.lstrip("-").isdigit() else float(v)
        except ValueError:
            out[k] = v
    return out


def _run(plan_path: Path, args) -> int:
    try:
        plan = load_plan(plan_path)
    except PlanError as exc:
        print(f"plan error: {exc}", file=sys.stderr)
        return EXIT_INVALID
    reset_sim_groups()
    if args.seed is not None:
        for spec in plan.instruments.values():
            if spec.is_sim:
                spec.sim = dict(spec.sim or {})
                spec.sim["seed"] = args.seed
    quiet = args.quiet
    if not quiet:
        print(f"benchplan {__version__}  |  {plan.name}" + (f" v{plan.version}" if plan.version else ""))
        print(f"serial: {args.serial or '-'}   operator: {args.operator or '-'}   instruments: "
              + ", ".join(f"{a}={s.resource}" for a, s in plan.instruments.items()))
        print("-" * 72)
    runner = Runner(plan, serial=args.serial, operator=args.operator, variables=_parse_vars(args.var),
                    on_fail="abort" if args.abort_on_fail else None,
                    on_step=(lambda r: None) if quiet else (lambda r: print(console_line(r))))
    result = runner.run()
    paths = write_all(result, args.out, plan.slug)
    if not quiet:
        for line in summary_lines(result):
            print(line)
        print(f"report: {paths['html']}")
    elif args.json:
        pass
    if args.json:
        print(paths["json"].read_text(encoding="utf-8"))
    return {"PASS": EXIT_PASS, "FAIL": EXIT_FAIL}.get(result.status, EXIT_ERROR)


def _demo_plan_path() -> Path:
    return Path(str(ires.files("benchplan").joinpath("examples", "psu_regulation.yaml")))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="benchplan",
                                description="Run YAML test plans against SCPI instruments and get pass/fail reports.")
    p.add_argument("--version", action="version", version=f"benchplan {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_run_opts(sp):
        sp.add_argument("--serial", "-s", default="", help="unit serial number (available as {serial})")
        sp.add_argument("--operator", "-o", default="", help="operator name")
        sp.add_argument("--out", default="benchplan-results", help="output directory (default: ./benchplan-results)")
        sp.add_argument("--var", action="append", metavar="NAME=VALUE", help="override a plan variable (repeatable)")
        sp.add_argument("--abort-on-fail", action="store_true", help="stop at the first failing step")
        sp.add_argument("--seed", type=int, default=None, help="seed for simulated instruments (reproducible noise)")
        sp.add_argument("--quiet", "-q", action="store_true", help="no console output")
        sp.add_argument("--json", action="store_true", help="print the JSON result to stdout")

    sp_run = sub.add_parser("run", help="run a plan")
    sp_run.add_argument("plan", help="path to the YAML plan")
    add_run_opts(sp_run)

    sp_val = sub.add_parser("validate", help="check a plan file without running it")
    sp_val.add_argument("plan")

    sp_demo = sub.add_parser("demo", help="run the bundled example with simulated instruments")
    add_run_opts(sp_demo)

    sp_res = sub.add_parser("resources", help="list VISA resources")
    sp_res.add_argument("--visa-library", default="", help='e.g. "@py" for pyvisa-py')

    sp_ex = sub.add_parser("example", help="print the bundled example plan (copy it as a starting point)")

    args = p.parse_args(argv)

    if args.cmd == "run":
        return _run(Path(args.plan), args)
    if args.cmd == "demo":
        return _run(_demo_plan_path(), args)
    if args.cmd == "example":
        print(_demo_plan_path().read_text(encoding="utf-8"))
        return EXIT_PASS
    if args.cmd == "validate":
        problems = validate_plan(args.plan)
        if problems:
            for pr in problems:
                print(f"  - {pr}")
            print(f"{args.plan}: {len(problems)} problem(s)")
            return EXIT_INVALID
        plan = load_plan(args.plan)
        print(f"{args.plan}: OK  ({len(plan.steps)} steps, {len(plan.instruments)} instruments, "
              f"{sum(1 for s in plan.steps if s.has_limits)} with limits)")
        return EXIT_PASS
    if args.cmd == "resources":
        try:
            res = list_resources(args.visa_library)
        except InstrumentError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_ERROR
        print("\n".join(res) if res else "(no VISA resources found)")
        return EXIT_PASS
    return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
