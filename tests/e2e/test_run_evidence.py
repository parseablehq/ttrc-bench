"""`otb run new` places the gate evidence by content: runs of one session share one read-only copy of each raw
response file through hard links, and the frozen inputs still hash, rejudge and self-check as before. Built from
the real gate evidence of tests/fixtures/gate-slice (three stacks' raw probe responses) with a suite composed for
it, through the `otb` CLI.

Ways it could be wrong, written before the code:
 1. Two runs of one session each get their own copy of the raw responses (the dry run's were 175 MB compressed per
    run; a week's capture is far larger).
 2. A run links the session's own file, which a resumed gate appends to, so a frozen input changes under the run.
 3. The linked file's bytes or pre-registration hash differ from the session's file, or the offline gate rejudge
    from the linked evidence no longer reproduces gate.json.
 4. A second `run new` on the same run dir re-copies, relinks or refuses its own identical inputs; one whose
    evidence changed is not refused.
 5. The store's copy is writable, so an edit through one run's link changes every run.

Evidence: tests/e2e/artifacts/run_evidence.json.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from pathlib import Path

import yaml
from conftest import ok, otb, write_artifact

from otb.run.config import EVIDENCE_STORE
from otb.selfcheck import gate_rejudge_identical, inputs_match_prereg

REPO = Path(__file__).resolve().parents[2]
SLICE = REPO / "tests/fixtures/gate-slice"
RESPONSES = "gate/b1-lgtm/responses.jsonl.zst"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compose(tmp_path: Path) -> tuple[Path, Path]:
    """A suite and a session built from the gate slice: its instances, a truth per instance, its gate evidence."""
    suite, session = tmp_path / "suite", tmp_path / "session"
    suite.mkdir()
    shutil.copyfile(SLICE / "instances.jsonl", suite / "instances.jsonl")
    ids = [json.loads(line)["instance_id"] for line in (SLICE / "instances.jsonl").read_text().splitlines() if line]
    (suite / "truth.jsonl").write_text("".join(json.dumps({"instance_id": i, "answer": {}}) + "\n" for i in ids))
    corpus_id = json.loads((SLICE / "session.json").read_text())["corpus_id"]
    (suite / "suite.json").write_text(json.dumps({"corpus_id": corpus_id, "seed": 0, "dropped": []}) + "\n")
    shutil.copytree(SLICE, session, ignore=shutil.ignore_patterns("instances.jsonl"))
    return suite, session


def config(path: Path, run_id: str, suite: Path, session: Path) -> Path:
    cfg = {
        "run_id": run_id,
        "suite": str(suite),
        "session": str(session),
        "stacks": ["b1-lgtm", "b3-clickhouse", "b4-parseable"],
        "agent": {"adapter": "null", "variant": "abstain"},
        "k": 1,
        "budget": {"calls": 30, "call_timeout_s": 60, "wall_s": 300, "usd": 0.5},
        "ctx_cap_bytes": 45000,
    }
    path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return path


def test_runs_of_one_session_share_the_evidence_bytes(tmp_path: Path, artifacts: Path) -> None:
    suite, session = compose(tmp_path)
    runs = tmp_path / "runs"
    a, b = runs / "scratch-evidence-a", runs / "scratch-evidence-b"
    ok(otb("run", "new", a, "--config", config(tmp_path / "a.yaml", a.name, suite, session)))
    ok(otb("run", "new", b, "--config", config(tmp_path / "b.yaml", b.name, suite, session)))
    source = session / RESPONSES
    digest = sha(source)
    store = runs / EVIDENCE_STORE / digest
    linked = {r: (r / "inputs" / RESPONSES).stat() for r in (a, b)}
    assert sha(a / "inputs" / RESPONSES) == sha(b / "inputs" / RESPONSES) == digest
    assert linked[a].st_ino == linked[b].st_ino == store.stat().st_ino, "one inode behind both runs and the store"
    assert linked[a].st_nlink == 3 and linked[a].st_ino != source.stat().st_ino, "never the session's own file"
    assert stat.S_IMODE(store.stat().st_mode) == 0o444
    evidence = sorted(str(p.relative_to(a / "inputs")) for p in (a / "inputs" / "gate").rglob("*") if p.is_file())
    assert evidence == [
        "gate/b1-lgtm/counters_start.json",
        "gate/b1-lgtm/ingest.json",
        "gate/b1-lgtm/responses.jsonl.zst",
        "gate/b3-clickhouse/counters_start.json",
        "gate/b3-clickhouse/ingest.json",
        "gate/b3-clickhouse/responses.jsonl.zst",
        "gate/b4-parseable/counters_start.json",
        "gate/b4-parseable/ingest.json",
        "gate/b4-parseable/responses.jsonl.zst",
        "gate/expected.json",
        "gate/plan.json",
    ]
    assert all(sha(a / "inputs" / e) == sha(session / e) for e in evidence)

    ok(otb("run", "register", a))
    prereg = json.loads((a / "prereg.json").read_text())["sha256"]
    assert prereg[f"inputs/{RESPONSES}"] == digest
    assert all(prereg[f"inputs/{e}"] == sha(session / e) for e in evidence)
    assert inputs_match_prereg(a)["status"] == "pass"
    assert gate_rejudge_identical(a) == {"status": "pass"}, "the rejudge replays the linked evidence byte for byte"

    ok(otb("run", "new", a, "--config", tmp_path / "a.yaml"))
    assert (a / "inputs" / RESPONSES).stat().st_ino == linked[a].st_ino, "a second run new keeps the link"
    assert inputs_match_prereg(a)["status"] == "pass"

    with source.open("ab") as f:
        f.write(b"\x00")
    refused = otb("run", "new", a, "--config", tmp_path / "a.yaml")
    assert refused.returncode != 0 and "differs from its source" in refused.stderr + refused.stdout
    assert sha(a / "inputs" / RESPONSES) == digest, "the session's append did not reach the run"
    c = runs / "scratch-evidence-c"
    ok(otb("run", "new", c, "--config", config(tmp_path / "c.yaml", c.name, suite, session)))
    assert sha(c / "inputs" / RESPONSES) == sha(source) != digest
    assert sorted(p.name for p in (runs / EVIDENCE_STORE).iterdir() if len(p.name) == 64) == sorted(
        {sha(r / "inputs" / e) for r in (a, c) for e in evidence}
    )
    try:
        with (a / "inputs" / RESPONSES).open("ab") as f:
            f.write(b"\x00")
        writable = True
    except PermissionError:
        writable = False
    assert not writable or os.geteuid() == 0, "the shared copy is read-only through every link"

    write_artifact(
        artifacts / "run_evidence.json",
        {
            "evidence": evidence,
            "shared_inode": linked[a].st_ino == linked[b].st_ino,
            "links_to_shared_copy": linked[a].st_nlink,
            "store_mode": oct(stat.S_IMODE(store.stat().st_mode)),
            "prereg_sha_equals_session_file": prereg[f"inputs/{RESPONSES}"] == digest,
            "gate_rejudge_identical": gate_rejudge_identical(a),
            "changed_session_file_refused": refused.returncode != 0,
            "store_blobs_after_change": sum(1 for p in (runs / EVIDENCE_STORE).iterdir() if len(p.name) == 64),
        },
    )
