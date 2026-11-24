"""The capture gateway as a subprocess (`otb capture --gateway-only`), and the corpus-mini frames to feed it. The
corpus-mini events are older than any archive a test creates, so the gateway runs with --stale-s inf unless a test
passes its own."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import orjson

from otb.archive import Archive

REPO = Path(__file__).resolve().parents[2]
MINI = REPO / "tests/fixtures/corpus-mini"


def mini_frames() -> list[tuple[str, bytes]]:
    """(signal, body) of every corpus-mini frame in replay order."""
    a = Archive.open(MINI, verify="index")
    return [(ref.signal, a.read(ref, e)) for ref, e in a.merged()]


def post(url: str, signal: str, body: bytes, headers: dict[str, str] | None = None) -> tuple[int, str]:
    h = {"Content-Type": "application/json"} | (headers or {})
    req = urllib.request.Request(f"{url}/v1/{signal}", data=body, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def health(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=2) as r:
            return orjson.loads(r.read())
    except OSError:
        return None


class GatewayProc:
    def __init__(self, out: Path, port: int, log: Path, *extra: str) -> None:
        self.out = out
        self.url = f"http://127.0.0.1:{port}"
        self.argv = [sys.executable, "-m", "otb.cli", "capture", "--gateway-only", "--out", str(out)]
        self.argv += ["--listen", f"127.0.0.1:{port}", "--stale-s", "inf", *extra]
        self.log = log
        self.proc: subprocess.Popen | None = None

    def start(self, timeout_s: float = 20) -> None:
        with self.log.open("ab") as f:
            self.proc = subprocess.Popen(self.argv, cwd=REPO, stdout=f, stderr=subprocess.STDOUT, env=os.environ.copy())
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"gateway exited {self.proc.returncode}: {self.log.read_text()[-2000:]}")
            if health(self.url):
                return
            time.sleep(0.05)
        raise RuntimeError("gateway did not become healthy")

    def kill(self) -> None:
        assert self.proc is not None
        self.proc.send_signal(signal.SIGKILL)
        self.proc.wait()

    def term(self, timeout_s: float = 60) -> int:
        assert self.proc is not None
        self.proc.send_signal(signal.SIGTERM)
        return self.proc.wait(timeout=timeout_s)

    def reports(self) -> list[dict]:
        return [orjson.loads(p.read_bytes()) for p in sorted((self.out / "derived/gateway").glob("*.json"))]
