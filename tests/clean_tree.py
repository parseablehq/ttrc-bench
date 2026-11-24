"""pytest plugin: a test run must leave `git status` as it found it, unless OTB_WRITE_ARTIFACTS=1 asked to refresh
the committed evidence (tests/e2e/conftest.py)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BEFORE = pytest.StashKey[set[str] | None]()


def _status() -> set[str] | None:
    try:
        p = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return set(p.stdout.splitlines()) if p.returncode == 0 else None


def pytest_sessionstart(session: pytest.Session) -> None:
    session.config.stash[BEFORE] = None if os.environ.get("OTB_WRITE_ARTIFACTS") == "1" else _status()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    before = session.config.stash.get(BEFORE, None)
    after = _status() if before is not None else None
    if before is None or after is None or after == before:
        return
    changed = sorted(after ^ before)
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line("the test run changed the working tree (tests must write to tmp dirs):", red=True)
        for line in changed:
            reporter.write_line(f"  {line}", red=True)
    session.exitstatus = pytest.ExitCode.TESTS_FAILED
