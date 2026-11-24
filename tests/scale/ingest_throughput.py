"""Ingest throughput with sends: `otb ingest`'s feeder processes, as fast as stand-in collectors ack.

  uv run python tests/scale/ingest_throughput.py CORPUS [--stacks 4] [--max-inflight 4] [--window START/END]
      [--scratch DIR] [--out FILE]

Runs `otb.ingest.run_session` (one feeder process per stack, real ledgers, real HTTP) against --stacks stand-in
collectors (tests/e2e/fake_collector.py, one process each, answering 200 at once). The session's shift is minus
(recording span + 4 days), so every frame is already due: each feeder prepares and sends as fast as it can, and the
leftover recording-time scan runs as in a real replay. Session state goes to a scratch repo, never to state/.

Reported: each feeder's MB/s over its own wall time and per CPU second, its peak RSS, and the aggregate MB/s over
the whole session (sum of raw bytes sent by all stacks / session wall). The stand-in collector hashes and logs each
body; if it is slower than a feeder, the result is a lower bound. Writes CORPUS/derived/scale/ingest_throughput.json
unless --out is given; `otb scale-report` reads it from there. Needs no Docker and no running stack.
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from otb.archive import Archive
from otb.host import describe
from otb.ingest import (
    Target,
    feeder_stats_path,
    parse_window,
    read_ledger,
    run_session,
    selected_frames,
    start_session,
)
from otb.timebase import NS

REPO = Path(__file__).resolve().parents[2]
DAY_S = 86_400
PATHS = {"traces": "/v1/traces", "logs": "/v1/logs", "metrics": "/v1/metrics"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_listening(port: int, deadline_s: float = 20.0) -> None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < deadline_s:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"stand-in collector on :{port} did not start")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("corpus", type=Path)
    p.add_argument("--stacks", type=int, default=4)
    p.add_argument("--max-inflight", type=int, default=4, help="concurrent POSTs per feeder, as stack.yaml sets")
    p.add_argument("--window", default=None, help="START/END in recording time: a slice")
    p.add_argument("--scratch", type=Path, default=None, help="session state and collector logs (default: a temp dir)")
    p.add_argument("--out", type=Path, default=None)
    a = p.parse_args()
    out = a.out or a.corpus / "derived" / "scale" / "ingest_throughput.json"
    corpus = a.corpus.resolve()
    window = parse_window(a.window) if a.window else None
    archive = Archive(corpus)
    span = archive.manifest.window
    first_due = min(e.max_event_ns for _, e in selected_frames(archive, window))
    back_s = (span.end - span.start) // NS + 4 * DAY_S
    names = [f"s{i}" for i in range(a.stacks)]

    with tempfile.TemporaryDirectory(prefix="otb-ingest-") as tmp:
        scratch = a.scratch or Path(tmp)
        scratch.mkdir(parents=True, exist_ok=True)
        ports = {n: free_port() for n in names}
        serve = [sys.executable, str(REPO / "tests/e2e/fake_collector.py"), "serve"]
        collectors = [
            subprocess.Popen([*serve, str(port), str(scratch / f"{n}.log")], stdout=subprocess.DEVNULL)
            for n, port in ports.items()
        ]
        try:
            for port in ports.values():
                wait_listening(port)
            session, _ = start_session(
                corpus, tuple(names), "replay", window=window, lead_s=0, repo=scratch, now_ns=first_due - back_s * NS
            )
            targets = [
                Target(n, f"http://127.0.0.1:{port}", PATHS, a.max_inflight, dict.fromkeys(PATHS))
                for n, port in ports.items()
            ]
            t0 = time.perf_counter()
            reports = run_session(session, targets, scratch, log=lambda m: print(m, flush=True))
            wall = time.perf_counter() - t0
        finally:
            for c in collectors:
                c.terminate()
                c.wait()
        feeders = {}
        for n in names:
            (stats,) = [json.loads(x) for x in feeder_stats_path(session, n, scratch).read_text().splitlines()]
            retries = sum(1 for x in read_ledger(session.ledger_path(n, scratch)) if x["ev"] == "retry")
            feeders[n] = {k: stats[k] for k in ("frames", "raw_bytes", "wall_s", "raw_mb_per_s", "raw_mb_per_cpu_s")}
            feeders[n] |= {"peak_rss_bytes": stats["peak_rss_bytes"], "retries": retries}
            feeders[n] |= {"finished": reports[n].finished, "complete": reports[n].complete}
    total = sum(f["raw_bytes"] for f in feeders.values())
    doc = {
        "benchmark": "ingest_throughput",
        "corpus": corpus.name,
        "corpus_id": archive.manifest.corpus_id,
        "window": None if window is None else [window.start, window.end],
        "host": describe(),
        "stacks": a.stacks,
        "max_inflight": a.max_inflight,
        "shift_s": session.shift_s,
        "session_wall_s": round(wall, 2),
        "aggregate_raw_mb_per_s": round(total / wall / 1e6, 1),
        "feeders": feeders,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=1) + "\n")
    print(json.dumps(doc, indent=1))
    return 0 if all(f["finished"] for f in feeders.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
