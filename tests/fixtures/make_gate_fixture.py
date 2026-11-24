"""Cut tests/fixtures/gate-slice/ from a gated session: real stack answers small enough for CI to rejudge offline.

  uv run python tests/fixtures/make_gate_fixture.py state/sessions/<id> --suite suites/<suite> [--per-family 3]

Points kept, with their raw recorded responses:
- a few of every family per stack;
- every point that is not `pass` on some stack, and the per-service siblings of a failed all-services count
  (the gate localises that failure from them);
- every point whose check needed traces fetched by id, under a size cap, and the recorded fetches;
- per suite instance, the smallest windowed point that overlaps its footprint, so each instance stays covered.
The suite's instances.jsonl is copied next to the gate, so the exclusion table can be re-derived offline.
Only the session's stacks that this checkout defines are cut: rejudge needs each one's probes.py. Points are chosen
over every stack the session gated, so dropping a stack from the registry leaves the plan unchanged.
"""

from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import replace
from pathlib import Path

from otb.footprint import Region, overlaps
from otb.gate import FETCH, RESPONSES, Plan, append_response, read_responses, rejudge
from otb.ingest import Session
from otb.stacks import registry
from otb.tasks.suite import load_instances

OUT = Path(__file__).parent / "gate-slice"
MAX_RESPONSE_BYTES = 100_000  # per point and stack; Tempo span searches over a minute run to megabytes
MAX_KEPT_BYTES = 1_000_000  # the cap for points kept because they failed or used a fetch by id


def sibling_key(cid: str) -> str:
    """A check id without its service: the per-service counts of a window share it with their total."""
    kind, rest = cid.split(":", 1)
    if not rest.startswith("{"):
        return cid
    fields, window = rest.rsplit("@", 1)
    return f"{kind}:{json.dumps(json.loads(fields) | {'service': None}, sort_keys=True)}@{window}"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("session", type=Path)
    p.add_argument("--suite", type=Path, required=True)
    p.add_argument("--per-family", type=int, default=3)
    a = p.parse_args()
    src = a.session / "gate"
    plan_raw = json.loads((src / "plan.json").read_text())
    gate = json.loads((src / "gate.json").read_text())
    size: dict[str, int] = {}
    for d in (x for x in src.iterdir() if x.is_dir()):
        for r in read_responses(d / RESPONSES):
            size[r["id"]] = max(size.get(r["id"], 0), len(json.dumps(r)))
    checks = {cid: c for v in gate["stacks"].values() for cid, c in v["checks"].items()}
    keep: set[str] = set()
    taken: dict[str, int] = {}
    for pt in plan_raw["points"]:
        if size.get(pt["id"], 0) <= MAX_RESPONSE_BYTES and taken.get(pt["family"], 0) < a.per_family:
            taken[pt["family"]] = taken.get(pt["family"], 0) + 1
            keep.add(pt["id"])
    for v in gate["stacks"].values():
        for cid, c in v["checks"].items():
            if cid in size and size[cid] <= MAX_KEPT_BYTES and (c["status"] != "pass" or "search_incomplete" in c):
                keep.add(cid)
            if c.get("localised_to") is not None:
                keep |= {other for other in v["checks"] if sibling_key(other) == sibling_key(cid)}
    for inst in load_instances(a.suite / "instances.jsonl").values():
        near = [
            cid
            for cid, c in checks.items()
            if cid in size
            and c.get("region", {}).get("window") is not None
            and any(overlaps(Region.from_json(c["region"]), r) for r in inst.footprint.regions)
        ]
        keep.add(min(near, key=lambda cid: (size[cid], cid)))
    points = [pt for pt in plan_raw["points"] if pt["id"] in keep]
    shutil.rmtree(OUT, ignore_errors=True)
    (OUT / "gate").mkdir(parents=True)
    defined = registry()
    session = Session.load(a.session / "session.json")
    (OUT / "session.json").write_bytes(
        replace(session, stacks=tuple(s for s in session.stacks if s in defined)).to_json()
    )
    shutil.copyfile(a.suite / "instances.jsonl", OUT / "instances.jsonl")
    (OUT / "gate" / "plan.json").write_text(json.dumps(plan_raw | {"points": points}, indent=1, sort_keys=True) + "\n")
    reference = json.loads((src / "expected.json").read_text())["reference"]
    expected = {"reference": {k: v for k, v in reference.items() if k in keep}}
    (OUT / "gate" / "expected.json").write_text(json.dumps(expected, indent=1, sort_keys=True) + "\n")
    for d in sorted(x for x in src.iterdir() if x.is_dir() and x.name in defined):
        dst = OUT / "gate" / d.name
        dst.mkdir()
        for name in ("ingest.json", "counters_start.json"):
            shutil.copyfile(d / name, dst / name)
        last = {r["id"]: r for r in read_responses(d / RESPONSES) if r["id"] in keep or r["id"].startswith(FETCH)}
        with (dst / RESPONSES).open("wb") as f:
            for i in sorted(last):
                append_response(f, last[i])
    Plan.load(OUT / "gate" / "plan.json")
    got, _ = rejudge(OUT)
    (OUT / "gate" / "gate.json").write_bytes(got)
    print(f"{OUT}: {len(points)} points, {sum(1 for _ in (OUT / 'gate').rglob(RESPONSES))} stacks")


if __name__ == "__main__":
    main()
