"""A scenario's background volume through `otb scenario bytes`, and the 1 TB hybrid scenario's own guarantees.

Ways this can fail, each checked below: the background is parsed but left out of the total; its logs/metrics split
is misprinted; a malformed background entry (missing key, share outside [0, 1], typo'd key) is accepted silently;
tb-hybrid drifts off 1 TB; tb-hybrid schedules a fault whose symptom is unverified; its plan does not fit 24 h.

Evidence: tests/e2e/artifacts/scenario-background.json.
"""

from pathlib import Path

import yaml
from conftest import REPO, otb, write_artifact

from otb.scenario import Catalogue

TB = "scenarios/tb-hybrid.yaml"


def bad_scenario(tmp_path: Path, background: dict) -> str:
    d = yaml.safe_load((REPO / TB).read_text()) | {"background": background}
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(d))
    r = otb("scenario", "bytes", path)
    return f"exit {r.returncode}: {r.stderr.strip().replace(str(path), '<path>')}"


def test_background_bytes(artifacts: Path, tmp_path: Path) -> None:
    printed = otb("scenario", "bytes", TB)
    plan = yaml.safe_load(otb("scenario", "plan", TB).stdout)
    catalogue = Catalogue.load()
    flags = {e["flag"] for e in plan["entries"] if e["kind"] == "fault"}
    result = {
        "bytes": printed.stdout.strip().splitlines()[2:],
        "fault_flags": sorted(flags),
        "fault_flags_unverified": sorted(f for f in flags if catalogue.faults[f].symptom.unverified),
        "kinds": sorted(e["kind"] for e in plan["entries"]),
        "plan_ends_within_duration": max(e["end_offset_s"] for e in plan["entries"]) <= plan["duration_s"],
        "missing_share": bad_scenario(tmp_path, {"mb_per_s": 1}),
        "share_out_of_range": bad_scenario(tmp_path, {"mb_per_s": 1, "logs_share": 1.5}),
        "typo_key": bad_scenario(tmp_path, {"mb_per_sec": 1, "logs_share": 0.5}),
    }
    write_artifact(artifacts / "scenario-background.json", result)

    assert result == {
        "bytes": [
            "       0    24      8  6.47+2.33+5.62 = 14.41 x 1 x 1440 min =     20.8",
            "  background 11.33 MB/s (logs 60%, metrics 40%) x 1440 min = 978.9 GB",
            "  expected OTLP/JSON total: 999.7 GB (1.000 TB)",
        ],
        "fault_flags": ["paymentFailure"],
        "fault_flags_unverified": [],
        "kinds": ["decoy"] * 3 + ["fault"] * 6 + ["load"],
        "plan_ends_within_duration": True,
        "missing_share": "exit 1: otb scenario: <path>: background needs exactly mb_per_s and logs_share, got"
        " ['mb_per_s']",
        "share_out_of_range": "exit 1: otb scenario: <path>: background {'logs_share': 1.5, 'mb_per_s': 1}: mb_per_s"
        " must be >= 0 and logs_share in [0, 1]",
        "typo_key": "exit 1: otb scenario: <path>: background needs exactly mb_per_s and logs_share, got"
        " ['logs_share', 'mb_per_sec']",
    }
