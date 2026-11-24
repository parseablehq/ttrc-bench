"""Offline: the committed Kubernetes files that restate a repo config or a profile budget still restate it."""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def test_generated_files_match_their_sources() -> None:
    p = subprocess.run(
        [sys.executable, str(REPO / "deploy/k8s/stacks/generate.py"), "--check"], capture_output=True, text=True
    )
    assert p.returncode == 0, p.stdout + p.stderr
