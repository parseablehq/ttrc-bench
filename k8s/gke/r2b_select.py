"""Run 2b's question set (benchmark/RUN2B-PREREGISTRATION.md): 30 instances of the run-2 suite, each of which passed
every check of tests/independent/question_gates.py, none of which run 2 asked. The allow list holds those instances
outside the held-out split, and adds held-out instances only of a template and control kind whose rows the others
cannot fill. pick_r2.py then fills k8s/gke/r2b-table.json from the allow list with its usual rule.

    python k8s/gke/r2b_select.py <suite dir> <gates json>[,<gates json>...] <run-2 run.yaml> <schedule.yaml> <out dir>

An instance passes when every gates file that holds it says so. results/gke-r2b/review-rejected.json names the
instances the blind review rejected, with the reason; none of them is a candidate.
"""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

import pick_r2
import yaml

TABLE = Path(__file__).with_name("r2b-table.json")
REJECTED = Path(__file__).resolve().parents[2] / "results/gke-r2b/review-rejected.json"
SEED = 20261103


def main(suite: Path, gates: str, run2: Path, schedule: Path, out: Path) -> int:
    verdicts = collections.defaultdict(list)
    for path in gates.split(","):
        for i, r in json.loads(Path(path).read_text())["instances"].items():
            verdicts[i].append(r)
    rejected = json.loads(REJECTED.read_text()) if REJECTED.exists() else {}
    passed = {i: rs[0] for i, rs in verdicts.items() if all(r["pass"] for r in rs) and i not in rejected}
    used = set(yaml.safe_load(run2.read_text())["instances"])
    held = set(json.loads((suite / "suite.json").read_text())["held_out"]["ids"])
    need = collections.Counter()
    for row in json.loads(TABLE.read_text()):
        need[(row["templates"][0], row["control"])] += row["n"]
    fresh = collections.Counter((r["template"], r["control"]) for i, r in passed.items() if i not in used | held)
    short = {k for k, n in need.items() if fresh[k] < n}
    allow = sorted(
        i for i, r in passed.items() if i not in used and (i not in held or (r["template"], r["control"]) in short)
    )
    out.mkdir(parents=True, exist_ok=True)
    (out / "allow.json").write_text(json.dumps(allow, indent=1) + "\n")
    print(f"allow {len(allow)}: held-out added for {sorted(short)}")
    return pick_r2.main(
        [
            "--suite", str(suite), "--schedule", str(schedule), "--out", str(out), "--table", str(TABLE),
            "--allow", str(out / "allow.json"), "--seed", str(SEED), "--held-out-ok",
        ]
    )  # fmt: skip


if __name__ == "__main__":
    a = sys.argv[1:6]
    sys.exit(main(Path(a[0]), a[1], Path(a[2]), Path(a[3]), Path(a[4])))
