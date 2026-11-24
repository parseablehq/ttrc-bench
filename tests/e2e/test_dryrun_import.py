"""U1 check on the real dry-run recording: import, independent count, full verify, determinism, kill and resume.

Needs the v2 recording (561 MB, not in git); skipped without it. Evidence: tests/e2e/artifacts/dryrun_import.json.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml
from conftest import write_artifact

REPO = Path(__file__).resolve().parents[2]
V2 = Path(os.environ.get("OTB_V2_DRYRUN", str(REPO / "corpus/dryrun")))
ROTATE_MB = "4"

pytestmark = pytest.mark.skipif(not (V2 / "recording/traces.jsonl").exists(), reason="dry-run recording not present")


def otb(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "otb.cli", *args], cwd=REPO, capture_output=True, text=True)


def import_to(out: Path) -> dict:
    r = otb("corpus", "import-v2", str(V2 / "corpus.yaml"), "--out", str(out), "--rotate-mb", ROTATE_MB)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def frames_indexed(out: Path, signal_: str) -> int:
    return sum(p.read_bytes().count(b"\n") for p in (out / "segments" / signal_ / "g0").glob("*.idx.jsonl"))


def kill_when(out: Path, signal_: str, frames: int) -> None:
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "otb.cli",
            "corpus",
            "import-v2",
            str(V2 / "corpus.yaml"),
            "--out",
            str(out),
            "--rotate-mb",
            ROTATE_MB,
        ],
        cwd=REPO,
    )
    while proc.poll() is None and (not (out / "segments" / signal_).exists() or frames_indexed(out, signal_) < frames):
        time.sleep(0.01)
    proc.send_signal(signal.SIGKILL)
    proc.wait()
    assert proc.returncode == -signal.SIGKILL, "import finished before it could be killed"


def test_dryrun_import(tmp_path: Path, artifacts: Path) -> None:
    raw = json.loads(
        subprocess.run(
            [sys.executable, str(REPO / "tests/independent/raw_count.py"), str(V2 / "recording")],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    first = import_to(tmp_path / "a")
    second = import_to(tmp_path / "b")

    killed = tmp_path / "killed"
    kills = [("traces", 300), ("traces", 900), ("logs", 500), ("metrics", 350)]
    for signal_, frames in kills:
        kill_when(killed, signal_, frames)
    resumed = import_to(killed)

    verify = otb("corpus", "verify", str(tmp_path / "a"), "--level", "full")
    result = {
        "raw_count": raw,
        "archive_totals": first["totals"],
        "frames_total": sum(t["frames"] for t in first["totals"].values()),
        "corpus_id_first": first["corpus_id"],
        "corpus_id_second": second["corpus_id"],
        "corpus_id_after_kills": resumed["corpus_id"],
        "kills_at": kills,
        "verify_full": verify.stdout.strip().splitlines()[-1].split(": ", 1)[-1],
        "rotate_mb": int(ROTATE_MB),
    }
    write_artifact(artifacts / "dryrun_import.json", result)

    assert result["frames_total"] == 2859
    for s in ("traces", "logs", "metrics"):
        assert first["totals"][s]["frames"] == raw[s]["lines"]
        assert first["totals"][s]["records"] == raw[s]["records"]
    dropped = {
        k: v
        for k, v in yaml.safe_load((tmp_path / "a" / "manifest.yaml").read_text())["normaliser_dropped"].items()
        if v
    }
    rules = ("flag_service_records", "flag_events", "flag_metric_points", "flag_attrs")
    assert dropped == {"empty_body_set": raw["logs"]["null_body"]} | {
        r: sum(raw[s][r] for s in ("traces", "logs", "metrics")) for r in rules
    }
    assert dropped == {
        "empty_body_set": 12,
        "flag_service_records": 23478,
        "flag_events": 11890,
        "flag_metric_points": 222,
        "flag_attrs": 1912,
    }
    assert verify.returncode == 0, verify.stdout
    assert first["corpus_id"] == second["corpus_id"] == resumed["corpus_id"]
    segs = (tmp_path / "a" / "segments.jsonl").read_bytes()
    assert segs == (tmp_path / "killed" / "segments.jsonl").read_bytes()
