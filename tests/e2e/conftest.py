"""Shared pieces of the E2E tests: the HTTP fixture server, the `otb` CLI, run.yaml writing, and where evidence goes.

Tests never write into the repo by default. Evidence JSON goes to a session temp dir, and run dirs stay in each
test's tmp_path. `OTB_WRITE_ARTIFACTS=1` refreshes the committed evidence in tests/e2e/artifacts/ and keeps run dirs
in .otb/e2e/. Committed evidence holds deterministic fields only; timing and throughput go to `<name>.timing.json`,
which no test reads back."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures"
RUN_DIRS = REPO / ".otb" / "e2e"
EVIDENCE = REPO / "tests" / "e2e" / "artifacts"
WRITE_ARTIFACTS = os.environ.get("OTB_WRITE_ARTIFACTS") == "1"
TOKEN = "fx-token-7f3a9c21d4e8"
ADMIN_PASSWORD = "fx-admin-5b0e17d2c93a"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def http_url() -> Iterator[str]:
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, str(FIXTURES / "mcp_fixture_server.py"), "--http", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError("fixture HTTP server did not start")
    yield f"http://127.0.0.1:{port}"
    proc.terminate()
    proc.wait()


@pytest.fixture
def env(http_url: str, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setenv("FIXTURE_TOKEN", TOKEN)
    monkeypatch.setenv("FIXTURE_HTTP_URL", http_url)
    monkeypatch.setenv("FIXTURE_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("OTB_CANARY_LEAK", "must-not-reach-vendor-servers")
    return dict(os.environ)


def otb(*args: object) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "otb.cli", *map(str, args)],
        cwd=REPO,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        timeout=600,
    )


def ok(p: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
    assert p.returncode == 0, f"exit {p.returncode}\nstdout:\n{p.stdout}\nstderr:\n{p.stderr}"
    return p


def write_config(
    path: Path,
    run_id: str,
    agent: dict,
    *,
    session: str = "session-mini",
    stacks: tuple[str, ...] = ("fixture-stdio", "fixture-http"),
    k: int = 1,
    **extra: object,
) -> Path:
    cfg = {
        "run_id": run_id,
        "suite": "tests/fixtures/suite-mini",
        "session": f"tests/fixtures/{session}",
        "stacks": list(stacks),
        "stacks_root": "tests/fixtures/stacks",
        "agent": agent,
        "k": k,
        "budget": {"calls": 30, "call_timeout_s": 60, "wall_s": 300, "usd": 0.5},
        "ctx_cap_bytes": 45000,
        "analysis": {
            "bootstrap_seed": 20260929,
            "resamples": 400,
            "primary": [
                {
                    "name": "F2/F1 success, L1",
                    "metric": "success",
                    "numerator": "fixture-http",
                    "denominator": "fixture-stdio",
                    "levels": [1],
                    "hypothesis": "gt1",
                }
            ],
        },
        **extra,
    }
    path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return path


@pytest.fixture(scope="session")
def artifacts(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if WRITE_ARTIFACTS:
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        return EVIDENCE
    return tmp_path_factory.mktemp("artifacts")


def write_artifact(path: Path, data: object, *, timing: dict | None = None, sort_keys: bool = False) -> None:
    path.write_text(json.dumps(data, indent=1, sort_keys=sort_keys) + "\n")
    if timing is not None:
        path.with_suffix(".timing.json").write_text(json.dumps(timing, indent=1, sort_keys=True) + "\n")


def keep(run_dir: Path, name: str) -> None:
    if not WRITE_ARTIFACTS:
        return
    dst = RUN_DIRS / name
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(run_dir, dst)
