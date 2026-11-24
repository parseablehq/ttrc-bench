"""The gate verdict is a pure function of recorded evidence, on real stack answers cut from a live session
(tests/fixtures/gate-slice, made by tests/fixtures/make_gate_fixture.py). No stack, corpus or network.

Ways the gate could fail, each asserted below:
- a rejudge differs from the recorded verdict (non-deterministic judge, probes that read anything but the
  recorded responses, a parse that changed since recording);
- a one-record loss in one stack's raw answer goes unnoticed, or changes more than that one check;
- a probe that asks for something other than what was recorded silently reuses a recording, including a
  query that changed only in its POST body (SQL) under the same URL;
- a frame torn by a killed gate breaks the rejudge;
- a rerun of the gate that rewrites the ingest report with a measured arrival horizon (query_horizon_s) moves the
  range the recorded B4 probes are replayed with, so their bodies no longer match; or a response that does carry
  its own horizon is replayed under another one and the difference goes unnoticed.

Evidence: tests/e2e/artifacts/gate_rejudge.json.
"""

import json
import shutil
from pathlib import Path

from conftest import write_artifact

from otb.gate import RESPONSES, append_response, read_responses, rejudge

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests/fixtures/gate-slice"


def rewrite(path: Path, records: list[dict]) -> None:
    with path.open("wb") as f:
        for r in records:
            append_response(f, r)


def lower_count(stack: str, body: str) -> str:
    d = json.loads(body)
    if stack == "b1-lgtm":
        d["data"]["result"][0]["value"][1] = str(int(float(d["data"]["result"][0]["value"][1])) - 1)
    elif stack == "b3-clickhouse":
        d["data"][0][0] = int(d["data"][0][0]) - 1
    else:
        d[0]["v"] -= 1
    return json.dumps(d)


def test_rejudge_identical_and_one_record_loss_fails(tmp_path: Path, artifacts: Path) -> None:
    got, recorded = rejudge(FIXTURE)
    gate = json.loads(recorded)
    result: dict = {"rejudge_identical": got == recorded, "stacks": sorted(gate["stacks"]), "perturbed": {}}
    for stack in gate["stacks"]:
        copy = tmp_path / stack
        shutil.copytree(FIXTURE, copy)
        checks = gate["stacks"][stack]["checks"]
        localised = {k.rsplit("@", 1)[1] for k, c in checks.items() if "localised_to" in c}
        target = next(
            k
            for k, c in checks.items()
            if k.startswith("CountLogs:")
            and c["status"] == "pass"
            and c["expected"] > 0
            and k.rsplit("@", 1)[1] not in localised  # a sibling of a localised total would also move that total
        )
        path = copy / "gate" / stack / RESPONSES
        lines = list(read_responses(path))
        rec = next(r for r in lines if r["id"] == target)
        rec["exchanges"][-1]["response"] = lower_count(stack, rec["exchanges"][-1]["response"])
        rewrite(path, lines)
        after = json.loads(rejudge(copy)[0])["stacks"]
        changed = sorted(
            f"{sid}::{cid}"
            for sid in gate["stacks"]
            for cid, c in gate["stacks"][sid]["checks"].items()
            if c != after[sid]["checks"][cid]
        )
        result["perturbed"][stack] = {
            "check": target,
            "expected": gate["stacks"][stack]["checks"][target]["expected"],
            "got_after": after[stack]["checks"][target]["got"],
            "status_after": after[stack]["checks"][target]["status"],
            "changed": changed,
        }
    copy = tmp_path / "moved-url"
    shutil.copytree(FIXTURE, copy)
    path = copy / "gate" / "b3-clickhouse" / RESPONSES
    lines = list(read_responses(path))
    lines[0]["exchanges"][0]["url"] += "&recorded_elsewhere=1"
    rewrite(path, lines)
    moved = json.loads(rejudge(copy)[0])["stacks"]["b3-clickhouse"]["checks"][lines[0]["id"]]
    result["moved_url"] = {"check": lines[0]["id"], "status": moved["status"], "reason": moved["reason"][:160]}
    body = tmp_path / "changed-body"
    shutil.copytree(FIXTURE, body)
    path = body / "gate" / "b3-clickhouse" / RESPONSES
    lines = list(read_responses(path))
    rec = next(r for r in lines if r["exchanges"][0].get("request"))
    rec["exchanges"][0]["request"] = rec["exchanges"][0]["request"].replace("SELECT count()", "SELECT count() + 0")
    rewrite(path, lines)
    changed = json.loads(rejudge(body)[0])["stacks"]["b3-clickhouse"]["checks"][rec["id"]]
    result["changed_body"] = {"check": rec["id"], "status": changed["status"], "reason": changed["reason"][:160]}
    torn = tmp_path / "torn"
    shutil.copytree(FIXTURE, torn)
    tail = torn / "gate" / "b4-parseable" / RESPONSES
    whole = tail.read_bytes()
    tail.write_bytes(whole + whole[:40])
    result["torn_trailing_frame_rejudge_identical"] = rejudge(torn)[0] == recorded
    rewritten = tmp_path / "rewritten-horizon"
    shutil.copytree(FIXTURE, rewritten)
    report = rewritten / "gate" / "b4-parseable" / "ingest.json"
    measured = json.loads(report.read_text()) | {"query_horizon_s": 100_000}
    report.write_text(json.dumps(measured, indent=1, sort_keys=True) + "\n")
    result["rewritten_report_rejudge_identical"] = rejudge(rewritten)[0] == recorded
    carried = tmp_path / "carried-horizon"
    shutil.copytree(FIXTURE, carried)
    path = carried / "gate" / "b4-parseable" / RESPONSES
    lines = list(read_responses(path))
    rec = next(r for r in lines if r["id"].startswith("CountLogs:"))
    rec["horizon_s"] = 100_000
    rewrite(path, lines)
    moved_horizon = json.loads(rejudge(carried)[0])["stacks"]["b4-parseable"]["checks"][rec["id"]]
    result["carried_horizon"] = {
        "check": rec["id"],
        "status": moved_horizon["status"],
        "reason": moved_horizon["reason"][:160],
    }
    write_artifact(artifacts / "gate_rejudge.json", result)

    assert result["torn_trailing_frame_rejudge_identical"]
    assert result["rewritten_report_rejudge_identical"], (
        "probes recorded before each response carried its horizon replay under the horizon they ran with, "
        "whatever a rerun wrote into the ingest report"
    )
    assert result["carried_horizon"]["status"] == "fail"
    assert result["carried_horizon"]["reason"].startswith("probe error: RuntimeError: replay: recorded body sha256")

    assert result["moved_url"]["status"] == "fail"
    assert result["moved_url"]["reason"].startswith("probe error: RuntimeError: replay: recorded")
    assert result["changed_body"]["status"] == "fail"
    assert result["changed_body"]["reason"].startswith("probe error: RuntimeError: replay: recorded body sha256")

    assert result["rejudge_identical"]
    assert result["stacks"] == ["b1-lgtm", "b3-clickhouse", "b4-parseable"]
    for stack, r in result["perturbed"].items():
        assert r["got_after"] == r["expected"] - 1
        assert r["status_after"] == "fail"
        assert r["changed"] == [f"{stack}::{r['check']}"]
