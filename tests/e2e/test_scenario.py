"""Scenario files through the CLI: catalogue against the pinned flagd file, plan determinism, byte arithmetic.

Evidence: tests/e2e/artifacts/scenario.json.
"""

import hashlib
import subprocess
import sys
from pathlib import Path

import yaml
from conftest import write_artifact

REPO = Path(__file__).resolve().parents[2]


def otb(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "otb.cli", *args], cwd=REPO, capture_output=True, text=True)


def plan_bytes(scenario: str, seed: int) -> bytes:
    r = otb("scenario", "plan", f"scenarios/{scenario}.yaml", "--seed", str(seed))
    assert r.returncode == 0, r.stderr
    return r.stdout.encode()


def gaps_ok(plan: dict, min_gap: int) -> bool:
    timed = [e for e in plan["entries"] if e["kind"] != "load"]
    for i, a in enumerate(timed):
        for b in timed[i + 1 :]:
            if a.get("group") and a.get("group") == b.get("group"):
                continue
            if a["start_offset_s"] < b["end_offset_s"] + min_gap and b["start_offset_s"] < a["end_offset_s"] + min_gap:
                return False
    return True


def test_scenarios(artifacts: Path) -> None:
    catalogue = otb("scenario", "check-catalogue", "tests/fixtures/flagd/demo.flagd.json")
    smoke7a, smoke7b, smoke8 = plan_bytes("smoke", 7), plan_bytes("smoke", 7), plan_bytes("smoke", 8)
    week_a, week_b, week_c = (plan_bytes("week-ladder", s) for s in (20261001, 20261001, 20261002))
    smoke = yaml.safe_load(smoke7a)
    week = yaml.safe_load(week_a)
    by_id = {e["slot_id"]: e for e in week["entries"]}
    week_bytes = otb("scenario", "bytes", "scenarios/week-ladder.yaml").stdout
    result = {
        "catalogue_check": catalogue.stdout.strip().replace(str(REPO) + "/", ""),
        "smoke_seed7_sha256": hashlib.sha256(smoke7a).hexdigest(),
        "smoke_same_seed_identical": smoke7a == smoke7b,
        "smoke_other_seed_differs": smoke7a != smoke8,
        "smoke_seed7": {
            e["slot_id"]: [e.get("flag"), e.get("variant"), e["start_offset_s"], e["end_offset_s"]]
            for e in smoke["entries"]
        },
        "week_same_seed_identical": week_a == week_b,
        "week_other_seed_differs": week_a != week_c,
        "week_min_gap_respected": gaps_ok(week, 1800),
        "week_overlap_group_overlaps": by_id["f4b"]["start_offset_s"] < by_id["f4a"]["end_offset_s"]
        and by_id["f4a"]["start_offset_s"] <= by_id["f4b"]["start_offset_s"],
        "week_expected_total": week_bytes.strip().splitlines()[-1].strip(),
    }
    write_artifact(artifacts / "scenario.json", result)

    assert result == {
        "catalogue_check": "catalogue matches tests/fixtures/flagd/demo.flagd.json",
        "smoke_seed7_sha256": result["smoke_seed7_sha256"],
        "smoke_same_seed_identical": True,
        "smoke_other_seed_differs": True,
        "smoke_seed7": {
            "load0": [None, None, 0, 0],
            "f1": ["cartFailure", "50%", 246, 476],
            "d1": [None, None, 646, 766],
        },
        "week_same_seed_identical": True,
        "week_other_seed_differs": True,
        "week_min_gap_respected": True,
        "week_overlap_group_overlaps": True,
        "week_expected_total": "expected OTLP/JSON total: 1054.9 GB (1.055 TB)",
    }
