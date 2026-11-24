"""Check (f) of U6 on a gated session: `otb gate --rejudge` re-derives gate.json byte for byte from the recorded
responses, and a one-record loss in one recorded response flips exactly that check to fail.

  uv run python tests/live/gate_perturb.py state/sessions/<id> [--out docs/v1/units/U6-gate-perturb.json]

For each of B2, B3 and B4 it copies the session dir, lowers the count in the raw response of one passing span
count by one (the stack's own JSON shape: ES|QL `values`, ClickHouse JSONCompact `data`, Parseable rows), rejudges
the copy offline, and diffs the verdict against the original.
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from pathlib import Path

from otb.gate import RESPONSES, append_response, read_responses, rejudge
from otb.stacks import REPO


def rewrite(path: Path, records: list[dict]) -> None:
    with path.open("wb") as f:
        for r in records:
            append_response(f, r)


def lower_by_one(stack: str, body: str) -> str:
    d = json.loads(body)
    if stack == "b3-clickhouse":
        d["data"][0][0] = int(d["data"][0][0]) - 1
    elif stack == "b4-parseable":
        d[0]["v"] -= 1
    else:
        raise ValueError(stack)
    return json.dumps(d)


def perturb(session: Path, stack: str, check: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / session.name
        shutil.copytree(session, copy)
        path = copy / "gate" / stack / RESPONSES
        lines = list(read_responses(path))
        last = max(i for i, r in enumerate(lines) if r["id"] == check)
        ex = lines[last]["exchanges"][-1]
        ex["response"] = lower_by_one(stack, ex["response"])
        rewrite(path, lines)
        got, recorded = rejudge(copy)
    before = json.loads(recorded)["stacks"]
    after = json.loads(got)["stacks"]
    flipped = {
        f"{sid}::{cid}": [c["status"], after[sid]["checks"][cid]["status"]]
        for sid in before
        for cid, c in before[sid]["checks"].items()
        if c != after[sid]["checks"][cid]
    }
    return {
        "stack": stack,
        "check": check,
        "expected": before[stack]["checks"][check]["expected"],
        "got_after": after[stack]["checks"][check]["got"],
        "changed_checks": flipped,
        "family_after": after[stack]["families"][before[stack]["checks"][check]["family"]],
        "signal_after": after[stack]["signals"]["traces"],
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("session", type=Path)
    p.add_argument("--out", type=Path, default=REPO / "docs/v1/units/U6-gate-perturb.json")
    a = p.parse_args()
    got, recorded = rejudge(a.session)
    gate = json.loads(recorded)
    result: dict = {"session": a.session.name, "rejudge_identical": got == recorded, "perturbations": []}
    for stack in ("b3-clickhouse", "b4-parseable"):
        if stack not in gate["stacks"]:
            continue
        checks = gate["stacks"][stack]["checks"]
        target = next(
            k for k, c in checks.items() if k.startswith("CountSpans:") and c["status"] == "pass" and c["expected"] > 0
        )
        result["perturbations"].append(perturb(a.session, stack, target))
    a.out.write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
