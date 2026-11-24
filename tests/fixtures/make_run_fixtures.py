"""Writes the hand-made fixtures the run-path tests use: suite-mini and two ingest sessions with gate verdicts.

  uv run python tests/fixtures/make_run_fixtures.py

suite-mini has one integer instance, one time instance, an L5 two-cause object_list instance and its decoy
twin (a negative control). Truth is in recording time; the fixture MCP server's canned answers agree with it.
session-mini shifts by one hour and fails logs.error_by_service on fixture-http, so the L5 cells there are
excluded. session-smoke has no shift and passes everything, for the real-agent smoke cell.
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).parent
NS = 10**9
T = 1_790_000_000 * NS  # 2026-09-21T14:13:20Z, recording time


def window(a: int, b: int) -> dict:
    return {"window.start": {"ns": T + a * NS, "precision": "s"}, "window.end": {"ns": T + b * NS, "precision": "s"}}


def region(signal: str, services: list[str] | None, a: int | None = None, b: int | None = None) -> dict:
    w = None if a is None else [T + a * NS, T + b * NS]  # type: ignore[operator]
    return {"signal": signal, "services": services, "window": w, "metrics": None}


def footprint(signals: list[str], services: list[str] | None, a: int, b: int) -> dict:
    return {"regions": [region(s, services, a, b) for s in signals], "trace_ids": []}


L5 = {
    "type": "object_list",
    "cap": 3,
    "item": {
        "service": {"type": "string"},
        "fault_class": {"type": "enum", "values": ["error", "latency", "resource", "none", "insufficient_evidence"]},
        "onset_at": {"type": "time", "precision": "min", "tolerance_s": 120},
    },
}
L5_STATEMENT = (
    "On-call was paged at {{report_time}} UTC: customers report failures and slowness on the storefront. "
    "Investigate the data between {{window.start}} and {{window.end}} UTC. List every distinct root cause you can "
    "establish from the telemetry. For each, give the service where it originates, its class, and the time (UTC, "
    "to the minute) its first symptom appeared. If the telemetry shows no incident, say so."
)
L5_COMMON = {
    "template_id": "fx-l5-two-concurrent",
    "level": 5,
    "k_design": 6,
    "s": 3,
    "signals": ["traces", "logs", "metrics"],
    "expected_winner": "unified",
    "statement": L5_STATEMENT,
    "fields": {"incident_occurred": {"type": "boolean"}, "causes": L5},
    "requires": ["spans.error_by_service", "logs.error_by_service", "metrics.series_last"],
}
L1_COMMON = {
    "level": 1,
    "k_design": 1,
    "s": 1,
    "signals": ["traces"],
    "expected_winner": "neutral",
    "control": "none",
    "twin_of": None,
    "times": window(-600, 600),
    "scope": [T - 600 * NS, T + 600 * NS],
    "requires": ["spans.error_by_service"],
    "schedule_refs": ["slot-1"],
    "footprint": footprint(["traces"], ["payment"], -600, 600),
}

INSTANCES = [
    {
        **L1_COMMON,
        "instance_id": "fx-l1-count--a1",
        "template_id": "fx-l1-count",
        "statement": "Between {{window.start}} and {{window.end}} UTC, how many server spans of the service "
        "`payment` ended with error status?",
        "fields": {"error_span_count": {"type": "integer"}},
    },
    {
        **L1_COMMON,
        "instance_id": "fx-l1-first-error--b1",
        "template_id": "fx-l1-first-error",
        "statement": "Between {{window.start}} and {{window.end}} UTC, at what time did the first error span of the "
        "service `payment` start? Give a full UTC timestamp to the second.",
        "fields": {"first_error_at": {"type": "time", "precision": "s", "tolerance_s": 1}},
    },
    {
        **L5_COMMON,
        "instance_id": "fx-l5-two-concurrent--c1",
        "control": "none",
        "twin_of": None,
        "times": {**window(-1800, 2400), "report_time": {"ns": T + 30 * NS, "precision": "min"}},
        "scope": [T - 1800 * NS, T + 2400 * NS],
        "schedule_refs": ["slot-1", "slot-2"],
        "footprint": footprint(L5_COMMON["signals"], None, -1800, 2400),  # type: ignore[arg-type]
    },
    {
        **L5_COMMON,
        "instance_id": "fx-l5-two-concurrent--d1",
        "control": "decoy",
        "twin_of": "fx-l5-two-concurrent--c1",
        "times": {**window(7200, 11400), "report_time": {"ns": T + 9000 * NS, "precision": "min"}},
        "scope": [T + 7200 * NS, T + 11400 * NS],
        "schedule_refs": ["decoy-1"],
        "footprint": footprint(L5_COMMON["signals"], None, 7200, 11400),  # type: ignore[arg-type]
    },
]

MEASURE = {"source": "measure", "measure": "span_errors", "params": {"service": "payment"}, "result_sha256": None}
TRUTH = [
    {"instance_id": "fx-l1-count--a1", "answer": {"error_span_count": 42}, "derivation": {"error_span_count": MEASURE}},
    {
        "instance_id": "fx-l1-first-error--b1",
        "answer": {"first_error_at": T + 400_000_000},
        "derivation": {"first_error_at": MEASURE},
    },
    {
        "instance_id": "fx-l5-two-concurrent--c1",
        "answer": {
            "incident_occurred": True,
            "causes": [
                {"service": "payment", "fault_class": "error", "onset_at": T + 50 * NS},
                {"service": "cart", "fault_class": "latency", "onset_at": T + 900 * NS},
            ],
        },
        "derivation": {"incident_occurred": {"source": "const"}, "causes": {"source": "schedule"}},
    },
    {
        "instance_id": "fx-l5-two-concurrent--d1",
        "answer": {"incident_occurred": False, "causes": []},
        "derivation": {"incident_occurred": {"source": "const"}, "causes": {"source": "const"}},
    },
]


def dump(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, sort_keys=True) + "\n")


def main() -> None:
    suite = HERE / "suite-mini"
    suite.mkdir(exist_ok=True)
    (suite / "instances.jsonl").write_text("".join(json.dumps(i, sort_keys=True) + "\n" for i in INSTANCES))
    (suite / "truth.jsonl").write_text("".join(json.dumps(t, sort_keys=True) + "\n" for t in TRUTH))
    dump(
        suite / "suite.json",
        {
            "corpus_id": "fixture",
            "templates_sha256": None,
            "seed": 7,
            "dropped": [],
            "note": "hand-written fixture for the run path, not generated by instantiate",
        },
    )
    families = {f: "pass" for f in ("spans.error_by_service", "logs.error_by_service", "metrics.series_last")}
    ledger = {
        f"ingest.ledger:{s}": {"family": "ingest.ledger", "signal": s, "status": "pass", "region": region(s, None)}
        for s in ("traces", "logs", "metrics")
    } | {
        f"count:{s}@fixture": {
            "family": "fixture",
            "signal": s,
            "status": "pass",
            "region": region(s, None, -3600, 12000),
        }
        for s in ("traces", "logs", "metrics")
    }
    ok = {
        "ingest_complete": True,
        "ingest_reasons": [],
        "families": families,
        "signals": {s: "pass" for s in ("traces", "logs", "metrics")},
        "checks": ledger,
    }
    error_logs = {
        "family": "logs.error_by_service",
        "signal": "logs",
        "status": "fail",
        "region": region("logs", None),
    }
    http_mini = {
        **ok,
        "families": {**families, "logs.error_by_service": "fail"},
        "signals": {**ok["signals"], "logs": "fail"},
        "checks": {**ledger, "CountLogs:error_logs@fixture": error_logs},
    }
    for name, shift_s, http in (("session-mini", 3600, http_mini), ("session-smoke", 0, ok)):
        dump(
            HERE / name / "session.json",
            {"id": name, "corpus_id": "fixture", "mode": "fixture", "shift_s": shift_s, "window": None},
        )
        dump(
            HERE / name / "gate" / "gate.json",
            {
                "corpus_id": "fixture",
                "session_id": name,
                "plan_sha256": None,
                "stacks": {"fixture-stdio": ok, "fixture-http": http},
            },
        )


if __name__ == "__main__":
    main()
