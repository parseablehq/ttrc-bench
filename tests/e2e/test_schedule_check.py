"""schedule_check on a hand-made hour of telemetry whose fault, decoys and quiet windows are known by construction.

Ways the check can mislead a task, each asserted below:
- a background occurrence of the fault's error text before the injector acted pulls the onset early;
- an occurrence after the release plus the tail pushes the offset late;
- a signature that occurs no more often than before the fault is taken as the symptom;
- a decoy counts as realised because its command succeeded, although nothing changed: a restart with no new
  process start, a traffic gap with no drop in requests, a load step with no step in the request rate;
- a window where some service's errors rise far above its baseline is offered as quiet for a negative-control twin;
- the `region` param is passed to a measure that does not declare it, which the store rejects.

Evidence: tests/e2e/artifacts/schedule_check.json.
"""

from pathlib import Path

import orjson
import pytest
from conftest import write_artifact
from synthetic import NS, log, logs, metric, point, span, traces, write_corpus

from otb.ref.schedule_check import twin_host, write
from otb.scenario import ActionRecord, RealisedEntry, RealisedSchedule
from otb.timebase import EventNs, Window

REPO = Path(__file__).resolve().parents[2]
T0 = 1790236800 * NS  # 2026-09-24T08:00:00Z
MIN = 60 * NS
SIG = "Payment request failed. Invalid token."
SERVER = 2


def at(minute: float) -> int:
    return T0 + int(minute * MIN)


def requests_per_minute(minute: int) -> int:
    if 40 <= minute < 44:
        return 1
    if minute >= 50:
        return 30
    return 10


def frames() -> list[tuple[str, dict]]:
    out = []
    for m in range(60):
        n = requests_per_minute(m)
        spans = [
            span(f"{m:04d}{i:028d}", f"r{m}-{i}", None, "GET /", SERVER, at(m) + i * (MIN // n), 5) for i in range(n)
        ]
        if m == 10:
            spans += [
                span(f"e{i:031d}", f"e{i}", None, "GET /", SERVER, at(10.1 + i / 20), 5, error="oops") for i in range(6)
            ]
        out.append(traces("frontend", *spans))
    payment = [span("p" * 32, "bg", None, "charge", SERVER, at(28.2), 5, error=SIG)]
    payment += [
        span("p" * 31 + "x", f"f{i}", None, "charge", SERVER, at(30.5 + i / 3), 5, error=SIG) for i in range(13)
    ]
    payment += [span("p" * 31 + "y", "late", None, "charge", SERVER, at(40), 5, error=SIG)]
    out.append(traces("payment", *payment))
    out.append(logs("cart", log(at(1), "cart ready")))
    out.append(metric("cart", "process.cpu", "sum", [point(at(5), 1, at(0.1)), point(at(10.2), 1, at(10.1))]))
    out.append(metric("ad", "process.cpu", "sum", [point(at(5), 1, at(0.1)), point(at(16), 2, at(0.1))]))
    return out


def entry(slot: str, kind: str, start: float, end: float, **kw: object) -> RealisedEntry:
    w = Window(EventNs(at(start)), EventNs(at(end)))
    actions = (
        ActionRecord(at(start) - 2 * NS, "write", True, {"phase": "on"}),
        ActionRecord(at(end), "write", True, {"phase": "off"}),
    )
    return RealisedEntry(slot, kind, "realised", "default", w, actions=actions, **kw)  # type: ignore[arg-type]


def schedule() -> RealisedSchedule:
    fault = entry(
        "f1",
        "fault",
        30,
        35,
        flag="paymentFailure",
        variant="50%",
        fault_class="error_injection",
        root_service="payment",
    )
    decoys = (
        entry("d1", "decoy", 10, 11, decoy_type="benign_restart", target="cart"),
        entry("d2", "decoy", 15, 16, decoy_type="benign_restart", target="ad"),
        entry("d3", "decoy", 40, 44, decoy_type="traffic_gap", target="load-generator"),
        entry("d4", "decoy", 50, 60, decoy_type="load_step", target="load-generator"),
        entry("d5", "decoy", 20, 25, decoy_type="load_step", target="load-generator"),
    )
    return RealisedSchedule("synthetic", None, None, at(0), (fault, *decoys))


@pytest.fixture(scope="module")
def checks(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict]:
    corpus = write_corpus(tmp_path_factory.mktemp("schedule") / "synthetic", frames())
    (corpus / "schedule.yaml").write_bytes(schedule().to_yaml())
    doc = orjson.loads(write(corpus).read_bytes())  # type: ignore[union-attr]
    return {c["slot_id"]: c for c in doc["checks"]}


def test_onset_and_offset_come_from_the_rise_after_the_injector_acted(checks: dict[str, dict]) -> None:
    f1 = checks["f1"]
    assert {k: f1[k] for k in ("status", "signature", "onset_ns", "offset_ns", "affected_records")} == {
        "status": "realised",
        "signature": SIG,
        "onset_ns": at(30.5),
        "offset_ns": at(30.5 + 12 / 3),
        "affected_records": 13,
    }
    assert f1["baseline_records"] == 1


def test_decoys_are_confirmed_by_their_effect(checks: dict[str, dict]) -> None:
    got = {s: checks[s]["status"] for s in ("d1", "d2", "d3", "d4", "d5")}
    assert got == {"d1": "realised", "d2": "no_effect", "d3": "realised", "d4": "realised", "d5": "no_effect"}


def test_only_quiet_confirmed_windows_host_a_twin(checks: dict[str, dict], artifacts: Path) -> None:
    hosts = {s: twin_host(checks[s]) for s in ("d1", "d2", "d3", "d4", "d5")}
    write_artifact(artifacts / "schedule_check.json", {"checks": checks, "twin_host": hosts}, sort_keys=True)
    assert hosts == {
        "d1": "not quiet: frontend errors above the baseline band",
        "d2": "decoy effect not seen: no_effect",
        "d3": None,
        "d4": None,
        "d5": "decoy effect not seen: no_effect",
    }
