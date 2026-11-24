"""`otb capture` end to end without Docker: the gateway and the injector against a stand-in flagd-ui, killed and
resumed, while a collector-like client posts the corpus-mini frames with retry.

Plan (offsets from capture start): f1 paymentFailure 50% on 2 s, off 10 s; f2 cartFailure 25% on 11 s, off 15 s;
f3 adFailure on 16 s, off 17 s; the capture ends at 22 s and drains for 1 s.
  run 1  SIGKILL at ~4 s, while f1 is on.
  run 2  resumes: f1's on is not repeated, its off runs at 10 s. SIGTERM at ~12.5 s, while f2 is on.
  (down until ~18 s, past f3's whole window)
  run 3  resumes: f3 is recorded as skipped; seals at 23 s.
  run 4  the same command again: already sealed, same corpus_id.

Ways it could fail, each asserted below:
- a resumed run re-applies an action already confirmed, or restarts the clock;
- SIGTERM leaves a fault on, or leaves its window open, so the schedule claims the planned end;
- a fault whose window passed while the capture was down is recorded as realised;
- a flag is left on at the end;
- frames posted across the restarts are lost or stored twice;
- the sealed archive does not verify, or its manifest does not hash the scenario and schedule files;
- a rerun of a sealed capture changes it.

Evidence: tests/e2e/artifacts/capture_e2e.json.
"""

import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import orjson
from conftest import free_port, ok, otb, write_artifact
from gateway_proc import MINI, health, mini_frames, post

from otb.archive import Archive
from otb.inject import read_events
from otb.scenario import RealisedSchedule

REPO = Path(__file__).resolve().parents[2]
SCENARIO = """id: capture-e2e
seed: 3
duration_s: 22
instances: [local]
min_gap_s: 0
slots:
  - { id: f1, kind: fault, flag_in: [paymentFailure], variant_in: ["50%"], start_s: { lo: 2, hi: 2 }, duration_s: { lo: 8, hi: 8 } }
  - { id: f2, kind: fault, flag_in: [cartFailure], variant_in: ["25%"], start_s: { lo: 11, hi: 11 }, duration_s: { lo: 4, hi: 4 } }
  - { id: f3, kind: fault, flag_in: [adFailure], variant_in: ["on"], start_s: { lo: 16, hi: 16 }, duration_s: { lo: 1, hi: 1 } }
"""  # noqa: E501
NS = 1_000_000_000


def test_capture(tmp_path: Path, artifacts: Path) -> None:
    scenario = tmp_path / "capture-e2e.yaml"
    scenario.write_text(SCENARIO)
    out = tmp_path / "corpus"
    flagd_port, gw_port = free_port(), free_port()
    flagd_writes = tmp_path / "flagd-writes.jsonl"
    fake = [str(REPO / "tests/fixtures/fake_flagd_ui.py"), str(flagd_port)]
    flagd = subprocess.Popen(
        [sys.executable, *fake, str(REPO / "tests/fixtures/flagd/demo.flagd.json"), str(flagd_writes)]
    )
    gw_url = f"http://127.0.0.1:{gw_port}"
    argv = [sys.executable, "-m", "otb.cli", "capture", str(scenario), "--out", str(out)]
    argv += ["--flagd-ui", f"http://127.0.0.1:{flagd_port}", "--listen", f"127.0.0.1:{gw_port}"]
    argv += ["--drain", "1", "--rotate-mb", "0.25", "--stale-s", "inf"]
    log = tmp_path / "capture.log"

    def start() -> subprocess.Popen:
        with log.open("ab") as f:
            proc = subprocess.Popen(argv, cwd=REPO, stdout=f, stderr=subprocess.STDOUT, env=os.environ.copy())
        while not health(gw_url):
            assert proc.poll() is None, log.read_text()
            time.sleep(0.05)
        return proc

    def capture_start() -> int:
        return orjson.loads((out / "capture.json").read_bytes())["capture_start_ns"]

    def wait_offset(s: float) -> None:
        time.sleep(max(0.0, (capture_start() + s * NS - time.time_ns()) / NS))

    frames = mini_frames()
    fed: list[int] = []

    def feed() -> None:
        for sig, body in frames:
            while post_or_none(gw_url, sig, body) != 200:
                time.sleep(0.1)
            fed.append(1)
            time.sleep(0.04)

    try:
        time.sleep(0.3)
        run1 = start()
        feeder = threading.Thread(target=feed)
        feeder.start()
        wait_offset(4)
        run1.send_signal(signal.SIGKILL)
        exit1 = run1.wait()
        start1 = capture_start()

        run2 = start()
        wait_offset(12.5)
        run2.send_signal(signal.SIGTERM)
        exit2 = run2.wait(timeout=60)
        start2 = capture_start()

        wait_offset(18)
        run3 = start()
        exit3 = run3.wait(timeout=60)
        feeder.join(timeout=5)
        manifest_after_3 = Archive(out).manifest
        exit4 = subprocess.run(argv, cwd=REPO, capture_output=True, text=True, timeout=60)
    finally:
        flagd.kill()

    verify = ok(otb("corpus", "verify", out, "--level", "full"))
    a = Archive.open(out, verify="full")
    m = a.manifest
    src = Archive.open(MINI, verify="index").manifest
    sched_bytes = (out / "schedule.yaml").read_bytes()
    sched = RealisedSchedule.from_yaml(sched_bytes)
    events = read_events(out / "events.jsonl")
    writes = [json.loads(line) for line in flagd_writes.read_text().splitlines()]
    t0 = start1

    entries = {e.slot_id: e for e in sched.entries}
    slots = ("f1", "f2", "f3")

    def outcome(slot: str) -> dict:
        e = entries[slot]
        return {
            "status": e.status,
            "actions": [f"{x.action}:{x.detail['phase']}:{'ok' if x.ok else 'no'}" for x in e.actions],
        }

    offsets = {
        slot: {
            "start_s": round((entries[slot].window.start - t0) / NS, 1),
            "end_s": round((entries[slot].window.end - t0) / NS, 1),
        }
        for slot in slots
    }

    result = {
        "exit_codes": [exit1, exit2, exit3, exit4.returncode],
        "rerun_says_sealed": "already sealed" in exit4.stdout,
        "capture_start_unchanged": start1 == start2 == capture_start(),
        **{slot: outcome(slot) for slot in slots},
        "flag_writes": [
            {k: v for k, v in w["variants"].items() if k in ("paymentFailure", "cartFailure", "adFailure")}
            for w in writes
        ],
        "f1_on_confirmed_events": sum(
            1 for e in events if e["slot_id"] == "f1" and e["action"] == "flag.confirm" and e["phase"] == "on"
        ),
        "verify": verify.stdout.strip().splitlines()[-1].split(": ", 1)[1],
        "frames_fed": len(fed),
        "frames_stored": sum(m.totals[s].frames for s in m.totals),
        "records": {s: m.totals[s].records for s in m.totals},
        "fixture_records": {s: src.totals[s].records for s in src.totals},
        "scenario_sha256_matches": m.scenario_sha256 == hashlib.sha256(scenario.read_bytes()).hexdigest(),
        "schedule_sha256_matches": m.schedule_sha256 == hashlib.sha256(sched_bytes).hexdigest(),
        "rerun_kept_corpus_id": manifest_after_3.corpus_id == m.corpus_id is not None,
    }
    write_artifact(artifacts / "capture_e2e.json", result, timing={"window_offsets_s": offsets})

    def near(x: float, want: float, tol: float = 1.5) -> bool:
        return abs(x - want) <= tol

    assert result["exit_codes"] == [-9, 1, 0, 0]
    assert near(offsets["f1"]["start_s"], 2) and near(offsets["f1"]["end_s"], 10), offsets["f1"]
    assert result["f1"]["status"] == "realised"
    assert result["f2"]["status"] == "realised" and near(offsets["f2"]["end_s"], 12.5), offsets["f2"]
    assert result["f3"]["status"] == "failed" and result["f3"]["actions"] == ["skip:on:no", "skip:off:no"]
    assert {k: v for k, v in result.items() if k not in ("exit_codes", "f1", "f2", "f3")} == {
        "rerun_says_sealed": True,
        "capture_start_unchanged": True,
        "flag_writes": [
            {"paymentFailure": "50%", "cartFailure": "off", "adFailure": "off"},
            {"paymentFailure": "off", "cartFailure": "off", "adFailure": "off"},
            {"paymentFailure": "off", "cartFailure": "25%", "adFailure": "off"},
            {"paymentFailure": "off", "cartFailure": "off", "adFailure": "off"},
        ],
        "f1_on_confirmed_events": 1,
        "verify": "full verify ok (0 problems)",
        "frames_fed": 232,
        "frames_stored": 232,
        "records": {"traces": 24323, "logs": 8394, "metrics": 40712},
        "fixture_records": {"traces": 24323, "logs": 8394, "metrics": 40712},
        "scenario_sha256_matches": True,
        "schedule_sha256_matches": True,
        "rerun_kept_corpus_id": True,
    }


def post_or_none(url: str, signal: str, body: bytes) -> int | None:
    try:
        return post(url, signal, body)[0]
    except OSError:
        return None
