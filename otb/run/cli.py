"""`otb run new|register|dispatch|grade|report|selfcheck RUN_DIR`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from otb.run.config import RunRefused


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="otb run")
    sub = p.add_subparsers(dest="verb", required=True)
    new = sub.add_parser("new", help="write run.yaml (from --config) and the frozen inputs/")
    new.add_argument("run_dir", type=Path)
    new.add_argument("--config", type=Path)
    sub.add_parser("register", help="pre-register hashes; idempotent").add_argument("run_dir", type=Path)
    d = sub.add_parser("dispatch", help="run every cell; idempotent, resumes")
    d.add_argument("run_dir", type=Path)
    d.add_argument("--only", default="", help="only cells whose id contains this")
    sub.add_parser("grade", help="grade/ from the run dir alone").add_argument("run_dir", type=Path)
    r = sub.add_parser("report", help="report/ from grade/")
    r.add_argument("run_dir", type=Path)
    r.add_argument("--compare", type=Path, help="a published run dir to check interval containment against")
    sub.add_parser("selfcheck", help="offline checks; writes selfcheck.json").add_argument("run_dir", type=Path)
    a = p.parse_args(argv)
    run_dir = a.run_dir.resolve()
    try:
        if a.verb == "new":
            from otb.run.config import new_run

            cfg = new_run(run_dir, a.config.resolve() if a.config else None)
            print(f"{run_dir}: run {cfg.run_id}, inputs frozen in inputs/")
        elif a.verb == "register":
            from otb.run.config import register

            prereg = register(run_dir)
            print(f"registered {len(prereg['sha256'])} hashes at {prereg['harness_commit'][:10]}")
        elif a.verb == "dispatch":
            from otb.run.dispatch import dispatch

            dispatch(run_dir, a.only)
        elif a.verb == "grade":
            from otb.grade import grade_run

            print(json.dumps(grade_run(run_dir), indent=1, sort_keys=True))
        elif a.verb == "report":
            from otb.report import compare, report_run

            print(report_run(run_dir))
            if a.compare:
                print(compare(run_dir, a.compare.resolve()))
        else:
            from otb.selfcheck import selfcheck

            out = selfcheck(run_dir)
            for name, c in out["checks"].items():
                detail = (
                    "" if c["status"] == "pass" else json.dumps({k: v for k, v in c.items() if k != "status"})[:300]
                )
                print(f"{c['status'].upper():5s} {name} {detail}")
            return 0 if out["pass"] else 1
    except RunRefused as e:
        print(f"otb run {a.verb}: refused: {e}", file=sys.stderr)
        return 2
    return 0
