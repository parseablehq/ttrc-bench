"""Re-judge run 2's gate offline after the counter check learned that a collector counts a rejected record twice
(benchmark/RUN2-PREREGISTRATION.md section 10, deviation 2). Rebuilds each gated stack's ingest report from its
ledger and the archive under the lateness bound the gate ran with, refuses if anything but `rejected_records`
changed, then re-derives gate.json from the recorded probe responses. No stack is queried.

    python k8s/gke/r2_rejudge.py state/sessions/<session id>
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from otb.gate import _dumps, as_gated, gated_stacks, rejudge
from otb.ingest import Session, Target, write_reports
from otb.stacks.definition import registry


def without_rejected_records(report: dict) -> dict:
    return {
        **report,
        "signals": {s: {k: v for k, v in x.items() if k != "rejected_records"} for s, x in report["signals"].items()},
    }


def main(session_dir: Path) -> int:
    session = Session.load(session_dir / "session.json")
    g = session_dir / "gate"
    stacks = [as_gated(s, g) for s in gated_stacks(g, registry())]
    reports = write_reports(session, [Target.of(s) for s in stacks])
    for s in stacks:
        path = g / s.id / "ingest.json"
        old = json.loads(path.read_text())
        new = json.loads(json.dumps(reports[s.id].to_json()))
        if without_rejected_records(new) != old:
            print(f"{s.id}: the rebuilt report differs from the gated one beyond rejected_records", file=sys.stderr)
            return 1
        shutil.copy(path, path.with_suffix(".json.before-deviation2"))
        path.write_bytes(_dumps(new))
        print(s.id, {sig: x["rejected_records"] for sig, x in new["signals"].items()})
    shutil.copy(g / "gate.json", g / "gate.json.before-deviation2")
    got, _ = rejudge(session_dir)
    (g / "gate.json").write_bytes(got)
    for sid, v in json.loads(got)["stacks"].items():
        fails = [k for k, c in v["checks"].items() if c["status"] == "fail"]
        print(
            sid,
            len(fails),
            "fail;",
            sum(c["region"]["window"] is None for c in (v["checks"][k] for k in fails)),
            "unwindowed",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
