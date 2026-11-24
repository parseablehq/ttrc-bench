"""A stand-in for a stack's ingest collector, and a driver that runs an ingest session against such collectors.

The collector records every POST (path, body sha, declared and received length, answer, arrival) to a JSONL file
and answers by rule: it reads and then drops the connection without an answer for the first `drop_first`
requests, answers 400 for bodies whose sha is listed in `reject`, `fail_code` (503 unless given) for the next
`fail_first` requests, otherwise 200 after `delay_s`. A request whose body stops short of its Content-Length (its
sender died mid-request) is recorded with answer null and not answered; it counts toward none of the rules.

  python tests/e2e/fake_collector.py serve PORT LOG [--reject SHA ...] [--fail-first N] [--delay-s S]
  python tests/e2e/fake_collector.py drive REPO CORPUS WINDOW LEAD_S NAME=URL ...
  python tests/e2e/fake_collector.py follow REPO CORPUS NAME=URL ...
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def serve(
    port: int,
    log: Path,
    reject: set[str],
    fail_first: int,
    delay_s: float,
    fail_code: int = 503,
    drop_first: int = 0,
) -> ThreadingHTTPServer:
    lock = threading.Lock()
    state = {"n": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            declared = int(self.headers["Content-Length"])
            body = self.rfile.read(declared)
            sha = hashlib.sha256(body).hexdigest()

            def record(code: int | None) -> None:
                line = {"path": self.path, "sha": sha, "declared": declared, "received": len(body), "code": code}
                line["at_ns"] = time.time_ns()
                with lock, log.open("a") as f:
                    f.write(json.dumps(line) + "\n")

            if len(body) < declared:
                record(None)
                self.close_connection = True
                return
            with lock:
                state["n"] += 1
                n = state["n"]
            if n <= drop_first:
                record(None)
                self.close_connection = True
                return
            code = 400 if sha in reject else fail_code if n <= drop_first + fail_first else 200
            time.sleep(delay_s if code == 200 else 0)
            record(code)
            text = b'{"error":"fake rejection"}' if code == 400 else b"{}"
            self.send_response(code)
            self.send_header("Content-Length", str(len(text)))
            self.end_headers()
            self.wfile.write(text)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def target(name: str, url: str):
    from otb.ingest import Target

    paths = {"traces": "/v1/traces", "logs": "/v1/logs", "metrics": "/v1/metrics"}
    return Target(name, url, paths, 4, {"traces": 10.0, "logs": 10.0, "metrics": 10.0})


def drive(repo: Path, corpus: Path, window: str, lead_s: int, targets: list[str]) -> None:
    from otb.ingest import parse_window, run_session, start_session

    ts = [target(*t.split("=", 1)) for t in targets]
    session, resumed = start_session(
        corpus, tuple(t.stack for t in ts), "replay", window=parse_window(window), lead_s=lead_s, repo=repo
    )
    print(json.dumps({"session": session.id, "resumed": resumed, "shift_s": session.shift_s}), flush=True)
    run_session(session, ts, repo, log=lambda m: print(m, flush=True))


def follow(repo: Path, corpus: Path, targets: list[str]) -> int:
    """`otb ingest --mode follow` against stand-in stacks: the same session, ledger and feeder code."""
    from otb.ingest import run_session, start_session, wait_for_archive

    ts = [target(*t.split("=", 1)) for t in targets]
    wait_for_archive(corpus, 30)
    session, resumed = start_session(corpus, tuple(t.stack for t in ts), "follow", repo=repo)
    print(json.dumps({"session": session.id, "resumed": resumed, "shift_s": session.shift_s}), flush=True)
    reports = run_session(session, ts, repo, log=lambda m: print(m, flush=True))
    return 0 if all(r.finished for r in reports.values()) else 1


def main() -> int | None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("port", type=int)
    s.add_argument("log", type=Path)
    s.add_argument("--reject", nargs="*", default=[])
    s.add_argument("--fail-first", type=int, default=0)
    s.add_argument("--delay-s", type=float, default=0.0)
    d = sub.add_parser("drive")
    d.add_argument("repo", type=Path)
    d.add_argument("corpus", type=Path)
    d.add_argument("window")
    d.add_argument("lead_s", type=int)
    d.add_argument("targets", nargs="+")
    f = sub.add_parser("follow")
    f.add_argument("repo", type=Path)
    f.add_argument("corpus", type=Path)
    f.add_argument("targets", nargs="+")
    a = p.parse_args()
    if a.cmd == "follow":
        return follow(a.repo, a.corpus, a.targets)
    if a.cmd == "serve":
        serve(a.port, a.log, set(a.reject), a.fail_first, a.delay_s)
        while True:
            time.sleep(3600)
    drive(a.repo, a.corpus, a.window, a.lead_s, a.targets)


if __name__ == "__main__":
    sys.exit(main())
