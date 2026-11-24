"""k8s/gke/supervise.sh end to end, offline: it keeps a capture gateway (`otb capture --gateway-only`) and a
follow-mode ingest session (tests/e2e/fake_collector.py follow, the session and ledger code `otb ingest --mode
follow` runs) going while the test SIGKILLs the ingest twice: once while the capture is live and once after it
sealed, while the ingest is still draining.

Ways it could fail, each asserted below:
- the supervisor does not notice the exit, does not restart the ingest, or restarts it without backoff;
- a restarted ingest begins a new session instead of resuming, before or after the seal;
- a frame acknowledged before a kill is sent again, or a frame is never sent;
- a body the sink received in full is not byte-identical to an archived frame (follow mode sends the archived
  bytes unshifted), or some archived frame never reaches it;
- the supervisor restarts the capture after the corpus sealed, or keeps restarting the ingest after it finished;
- the supervisor exits non-zero after a sealed corpus and a finished ingest, or never exits.

A kill can land while a feeder is mid-request. The sink then reads fewer bytes than the Content-Length declared
and records the request unanswered; that fragment is not a received body, and the resumed ingest sends the frame
again. How many requests a run cuts varies with load, so that count goes to supervise.timing.json.

Evidence: tests/e2e/artifacts/supervise.json.
"""

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path

from conftest import free_port, write_artifact
from fake_collector import serve
from gateway_proc import health, mini_frames, post

from otb.archive import Archive
from otb.ingest import Session, read_ledger

REPO = Path(__file__).resolve().parents[2]


def test_supervisor_restarts_a_killed_ingest_which_resumes(tmp_path: Path, artifacts: Path) -> None:
    corpus, logs = tmp_path / "corpus", tmp_path / "logs"
    gw_port, sink_port = free_port(), free_port()
    sink_log = tmp_path / "sink.log"
    sink = serve(sink_port, sink_log, set(), 0, 0.15)
    gw_url = f"http://127.0.0.1:{gw_port}"
    capture = [sys.executable, "-m", "otb.cli", "capture", "--gateway-only", "--out", str(corpus)]
    capture += ["--listen", f"127.0.0.1:{gw_port}", "--duration", "4", "--stale-s", "inf"]
    ingest = [sys.executable, str(REPO / "tests/e2e/fake_collector.py"), "follow", str(tmp_path), str(corpus)]
    ingest += [f"sink=http://127.0.0.1:{sink_port}"]
    env = os.environ | {
        "PYTHONPATH": str(REPO),
        "SUPERVISE_BACKOFF_MIN_S": "1",
        "SUPERVISE_BACKOFF_MAX_S": "4",
    }
    out = tmp_path / "supervise.out"
    with out.open("wb") as f:
        sup = subprocess.Popen(
            ["bash", str(REPO / "k8s/gke/supervise.sh"), str(corpus), str(logs), "--", *capture, "--", *ingest],
            cwd=REPO,
            env=env,
            stdout=f,
            stderr=subprocess.STDOUT,
        )

    def ingest_pids() -> list[int]:
        return [int(p) for p in re.findall(r"ingest started pid (\d+)", out.read_text())]

    def outcomes() -> int:
        sessions = list((tmp_path / "state/sessions").glob("*/ingest/sink.ledger.jsonl"))
        return sum(1 for x in read_ledger(sessions[0]) if x["ev"] == "outcome") if sessions else 0

    def until(cond, what: str, limit_s: float = 60) -> None:
        deadline = time.monotonic() + limit_s
        while not cond():
            assert sup.poll() is None, out.read_text()
            assert time.monotonic() < deadline, f"timed out waiting for {what}:\n{out.read_text()}"
            time.sleep(0.05)

    frames = mini_frames()
    try:
        until(lambda: health(gw_url) is not None, "the gateway")
        feeder = threading.Thread(target=lambda: [post(gw_url, s, b) for s, b in frames])
        feeder.start()
        until(lambda: outcomes() >= 5, "the first outcomes")
        live_kill = {"sealed": Archive(corpus).manifest.sealed, "outcomes": outcomes()}
        os.kill(ingest_pids()[0], signal.SIGKILL)
        until(lambda: len(ingest_pids()) == 2, "the first restart")
        feeder.join()
        until(lambda: Archive(corpus).manifest.sealed, "the seal")
        until(lambda: outcomes() > live_kill["outcomes"], "the resumed ingest to send")
        sealed_kill = {"sealed": Archive(corpus).manifest.sealed, "outcomes": outcomes()}
        os.kill(ingest_pids()[1], signal.SIGKILL)
        exit_code = sup.wait(timeout=180)
    finally:
        if sup.poll() is None:
            sup.kill()
        sink.shutdown()

    text = out.read_text()
    runs = [json.loads(line) for line in (logs / "corpus-ingest.log").read_text().splitlines() if '"resumed"' in line]
    (ledger_path,) = (tmp_path / "state/sessions").glob("*/ingest/sink.ledger.jsonl")
    session = Session.load(ledger_path.parents[1] / "session.json")
    ledger = read_ledger(ledger_path)
    resent = 0
    acked: set[tuple] = set()
    for x in ledger:
        if x["ev"] == "intent" and tuple(x["f"]) in acked:
            resent += 1
        if x["ev"] == "outcome":
            acked.add(tuple(x["f"]))
    archive = Archive(corpus)
    m = archive.manifest
    archived = {e.body_sha256 for _, e in archive.merged()}
    requests = [json.loads(line) for line in sink_log.read_text().splitlines()]
    received = Counter(r["sha"] for r in requests if r["code"] is not None)
    cut = [{"declared": r["declared"], "received": r["received"]} for r in requests if r["code"] is None]
    result = {
        "supervisor_exit": exit_code,
        "first_kill_while_live": not live_kill["sealed"],
        "second_kill_after_seal": sealed_kill["sealed"] and sealed_kill["outcomes"] < len(frames),
        "ingest_starts": len(ingest_pids()),
        "capture_starts": len(re.findall(r"capture started pid", text)),
        "ingest_exits": exits("ingest", text),
        "capture_exit": exits("capture", text),
        "noise_lines": [x for x in text.splitlines() if " supervise: " not in x and "Killed" not in x],
        "runs_resumed": [r["resumed"] for r in runs],
        "one_session": len({r["session"] for r in runs}) == 1,
        "session_took_sealed_corpus_id": session.corpus_id == m.corpus_id,
        "done_marker": (ledger_path.parent / "done.json").exists(),
        "frames_archived": sum(t.frames for t in m.totals.values()),
        "frames_with_an_outcome": len(acked),
        "acked_frames_resent": resent,
        "distinct_bodies_received": len(received),
        "bodies_not_archived": sorted(received.keys() - archived),
    }
    write_artifact(artifacts / "supervise.json", result, timing={"requests_cut_by_a_kill": cut})
    assert result == {
        "supervisor_exit": 0,
        "first_kill_while_live": True,
        "second_kill_after_seal": True,
        "ingest_starts": 3,
        "capture_starts": 1,
        "ingest_exits": [
            ["137", "restarting in 1 s"],
            ["137", "restarting in 2 s"],
            ["0", "the corpus is sealed"],
        ],
        "capture_exit": [["0", "the corpus is sealed"]],
        "noise_lines": [],
        "runs_resumed": [False, True, True],
        "one_session": True,
        "session_took_sealed_corpus_id": True,
        "done_marker": True,
        "frames_archived": 232,
        "frames_with_an_outcome": 232,
        "acked_frames_resent": 0,
        "distinct_bodies_received": 232,
        "bodies_not_archived": [],
    }


def exits(role: str, text: str) -> list[list[str]]:
    pattern = rf"{role} exited (\d+) after \d+ s; (restarting in \d+ s|the corpus is sealed)"
    return [list(x) for x in re.findall(pattern, text)]
