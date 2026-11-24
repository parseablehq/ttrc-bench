"""Which stack runs which instance, re-derived offline from real stack answers: the live-45b follow session's gate
(state/sessions/live-45b-follow-20261003T063606Z), cut to tests/fixtures/gate-slice by
tests/fixtures/make_gate_fixture.py, and its suite's instances.

Ways it could be wrong, each asserted below:
- the exclusion table differs from the one the live gate gave: B1 and B3 run every instance, B4 is excluded by its
  three late frames only;
- a late frame excludes every instance on its signal instead of those whose window holds its records;
- B1's payment span counts pass for a reason other than the fetch by id: without the recorded fetches they must
  fail one span short, and B1 must lose every instance those counts cover;
- a stack runs an instance no windowed check covers.

The narrowing of a failed all-services count is tested in tests/unit/test_gate_localise.py: this session has none.

Evidence: tests/e2e/artifacts/gate_exclusions.json.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from conftest import write_artifact

from otb.footprint import covered
from otb.gate import FETCH, RESPONSES, append_response, read_responses, rejudge
from otb.run.dispatch import exclusion
from otb.tasks.suite import load_instances

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests/fixtures/gate-slice"
B4_EXCLUDED = {
    "l1-metric-error-calls--2bb3072bed": "gate on b4-parseable: ingest.late:metrics/g0/000000#1 fail",
    "l2-latency-shift--2d2ce73b19": "gate on b4-parseable: ingest.late:traces/g0/000000#5 fail, "
    "ingest.late:traces/g0/000000#6 fail",
    "l4-blast-radius--170241ea0c": "gate on b4-parseable: ingest.late:traces/g0/000000#5 fail, "
    "ingest.late:traces/g0/000000#6 fail",
}
PAYMENT_FAULT = '{"errors_only":%s,"kind":null,"service":"payment"}@1791010560000000000-1791010980000000000'


def table(gate: dict) -> dict[str, dict[str, str | None]]:
    instances = load_instances(FIXTURE / "instances.jsonl")
    return {i: {s: exclusion(gate, s, inst) for s in sorted(gate["stacks"])} for i, inst in sorted(instances.items())}


def runs(t: dict, stack: str) -> list[str]:
    return [i for i, row in t.items() if row[stack] is None]


def test_exclusion_table_from_recorded_answers(tmp_path: Path, artifacts: Path) -> None:
    got, recorded = rejudge(FIXTURE)
    assert got == recorded
    gate = json.loads(got)
    t = table(gate)
    instances = load_instances(FIXTURE / "instances.jsonl")
    assert len(t) == 18
    for s in ("b1-lgtm", "b3-clickhouse"):
        assert runs(t, s) == sorted(t), s
    assert {i: row["b4-parseable"] for i, row in t.items() if row["b4-parseable"]} == B4_EXCLUDED
    assert all(
        covered(gate["stacks"][s]["checks"], inst.footprint) for s in gate["stacks"] for inst in instances.values()
    )

    b4 = gate["stacks"]["b4-parseable"]["checks"]
    late = {cid: (c["got"], c["region"]["window"]) for cid, c in b4.items() if c["status"] != "pass"}
    assert late == {
        "ingest.late:metrics/g0/000000#1": ("12.949 s", [1791009381692658655, 1791009394824000906]),
        "ingest.late:traces/g0/000000#5": ("14.28 s", [1791009380469101000, 1791009395486393200]),
        "ingest.late:traces/g0/000000#6": ("12.371 s", [1791009382403000000, 1791009395403000000]),
    }, "B4 fails only its late frames, all stored by 06:36:36"
    assert t["l1-log-needle--b08fdb7320"]["b4-parseable"] is None, "logs from 06:36:00, but no logs frame was late"
    assert t["l1-trace-lookup--f16a07ac71"]["b4-parseable"] is None, "traces from 06:36:49, after the late frames"

    b1 = gate["stacks"]["b1-lgtm"]["checks"]
    fault = {e: b1["CountSpans:" + PAYMENT_FAULT % e] for e in ("false", "true")}
    assert [(c["expected"], c["got"], c["status"]) for c in fault.values()] == [(49, 49, "pass"), (39, 39, "pass")]
    assert fault["true"]["search_incomplete"] == {
        "spans": ["5f965442481c6675"],
        "traces": ["922085dd3d21c54a1bf30994abc80e58"],
    }

    no_fetch = tmp_path / "no-fetch"
    shutil.copytree(FIXTURE, no_fetch)
    path = no_fetch / "gate" / "b1-lgtm" / RESPONSES
    kept = [r for r in read_responses(path) if not r["id"].startswith(FETCH)]
    with path.open("wb") as f:
        for r in kept:
            append_response(f, r)
    gate_bare = json.loads(rejudge(no_fetch)[0])
    b1_bare = gate_bare["stacks"]["b1-lgtm"]["checks"]
    bare = {e: b1_bare["CountSpans:" + PAYMENT_FAULT % e] for e in ("false", "true")}
    assert [(c["got"], c["status"]) for c in bare.values()] == [(48, "fail"), (38, "fail")]
    assert "search_incomplete" not in bare["true"]
    t_bare = table(gate_bare)
    assert runs(t_bare, "b1-lgtm") == [
        "l1-log-errors--582065586e",
        "l1-log-needle--b08fdb7320",
        "l1-metric-error-calls--2bb3072bed",
        "l2-rate-peak--6a1b43c811",
    ], "without the fetch every instance reading traces loses B1"

    write_artifact(
        artifacts / "gate_exclusions.json",
        {
            "rejudge_identical": got == recorded,
            "runs": {s: runs(t, s) for s in sorted(gate["stacks"])},
            "excluded": {s: sorted(i for i, row in t.items() if row[s]) for s in sorted(gate["stacks"])},
            "b4_late": late,
            "b1_search_incomplete": gate["stacks"]["b1-lgtm"]["search_incomplete"],
            "b1_payment_without_fetch": {e: [c["expected"], c["got"]] for e, c in bare.items()},
            "b1_runs_without_fetch": runs(t_bare, "b1-lgtm"),
        },
    )
