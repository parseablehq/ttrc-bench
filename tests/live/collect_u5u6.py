"""Copy the evidence of the U5-U6 live checks out of state/sessions (gitignored) into committed JSON.

  uv run python tests/live/collect_u5u6.py CHECK=SESSION_ID ... [--out docs/v1/units/U5-U6-sessions.json]

Per session: the session header, each stack's ingest report summary (frames, records, rejected, in doubt,
lateness, retries) and, when gated, the gate counts, signal and family verdicts and the defect naming of
tests/live/gate_explain.py.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from otb.ingest import sessions_dir
from otb.stacks import REPO


def summarise(sid: str) -> dict:
    d = sessions_dir() / sid
    out: dict = {"session": json.loads((d / "session.json").read_text()), "ingest": {}}
    for rep in sorted((d / "ingest").glob("*.report.json")):
        r = json.loads(rep.read_text())
        out["ingest"][r["stack"]] = {
            "finished": r["finished"],
            "complete": r["complete"],
            "rejected": r["rejected"],
            "in_doubt": r["in_doubt"],
            "signals": {
                s: {
                    k: v[k]
                    for k in (
                        "frames_ok",
                        "frames_expected",
                        "records_ok",
                        "records_expected",
                        "frames_rejected",
                        "late_frames",
                        "late_bound_s",
                        "late_p50_s",
                        "late_p99_s",
                        "late_max_s",
                        "retries",
                        "refused_records",
                    )
                    if k in v
                }
                for s, v in r["signals"].items()
            },
        }
    gate = d / "gate" / "gate.json"
    if gate.exists():
        g = json.loads(gate.read_text())
        out["gate"] = {
            "plan_sha256": g["plan_sha256"],
            "points": len(json.loads((d / "gate" / "plan.json").read_text())["points"]),
            "stacks": {
                s: {
                    "counts": v["counts"],
                    "signals": v["signals"],
                    "families": v["families"],
                    "ingest_reasons": v["ingest_reasons"],
                }
                for s, v in g["stacks"].items()
            },
        }
        explained = subprocess.run(
            [sys.executable, str(REPO / "tests/live/gate_explain.py"), str(d)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        out["gate"]["explained"] = {s: v["by_cause"] for s, v in json.loads(explained)["stacks"].items()}
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("pairs", nargs="+", help="CHECK=SESSION_ID")
    p.add_argument("--out", type=Path, default=REPO / "docs/v1/units/U5-U6-sessions.json")
    a = p.parse_args()
    data = json.loads(a.out.read_text()) if a.out.exists() else {}
    for pair in a.pairs:
        check, sid = pair.split("=", 1)
        data[check] = summarise(sid)
    a.out.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    print(f"{a.out}: {sorted(data)}")


if __name__ == "__main__":
    main()
