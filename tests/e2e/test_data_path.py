"""Offline data path on the committed corpus-mini fixture: import -> verify -> ref build -> measures.

The fixture's frames are written back out as a v2-style recording and imported again; the import must produce
the fixture's segment bytes exactly. Expected measure values are literals computed independently by harness v2's
answer_key.load and gate.reference over the raw 3-minute slice (tests/fixtures/make_corpus_mini.py), not by otb;
the schedule_check values by a stdlib scan of the slice for payment's "Invalid token" spans and WARN logs.
Normaliser n2 drops flagd and flagd-ui and every feature_flag metric, so the literals that counted them (spans,
traces, error spans by service, calls points, services with traces) were recomputed by a stdlib scan of the raw
slice with those records left out; tests/independent/raw_count.py gives the same span and record counts.
No Docker, no network. Evidence: tests/e2e/artifacts/data_path.json.
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import orjson
import yaml
from conftest import write_artifact

from otb.archive import Archive
from otb.ref.store import ReferenceStore
from otb.timebase import EventNs, Window, parse_utc

REPO = Path(__file__).resolve().parents[2]
MINI = REPO / "tests/fixtures/corpus-mini"
WINDOW = Window(EventNs(parse_utc("2026-09-24T08:13:00Z")), EventNs(parse_utc("2026-09-24T08:16:00Z")))
CALLS = "traces.span.metrics.calls"
PLACEORDER = "oteldemo.CheckoutService/PlaceOrder"


def otb(*args: str) -> subprocess.CompletedProcess:
    r = subprocess.run([sys.executable, "-m", "otb.cli", *args], cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    return r


def write_recording(src: Archive, out: Path) -> Path:
    (out / "recording").mkdir(parents=True)
    sha = {}
    for signal in ("traces", "logs", "metrics"):
        path = out / "recording" / f"{signal}.jsonl"
        with path.open("wb") as f:
            for ref, e in src.frames(signal, "g0"):
                f.write(src.read(ref, e) + b"\n")
        sha[signal] = hashlib.sha256(path.read_bytes()).hexdigest()
    cfg = {
        "recording": {s: f"recording/{s}.jsonl" for s in sha},
        "sha256": sha,
        "source": {"demo_repository_tag": "3.1.0"},
        "window_utc": ["2026-09-24T08:13:00", "2026-09-24T08:16:00"],
        "events": [{"from": "08:14:12", "to": "08:19:19", "kind": "fault", "flag": "paymentFailure", "value": "50%"}],
        "slice_of": {"corpus": "corpus/dryrun/corpus.yaml", "rule": "line max event time in window_utc"},
    }
    (out / "corpus.yaml").write_text(yaml.safe_dump(cfg))
    return out / "corpus.yaml"


def test_data_path(tmp_path: Path, artifacts: Path) -> None:
    corpus_yaml = write_recording(Archive.open(MINI, verify="full"), tmp_path / "v2")
    corpus = tmp_path / "corpus-mini"
    imported = json.loads(otb("corpus", "import-v2", str(corpus_yaml), "--out", str(corpus), "--rotate-mb", "1").stdout)
    otb("corpus", "verify", str(corpus), "--level", "full")
    build = json.loads(otb("ref", "build", str(corpus), "--workers", "2").stdout)

    fixture_segs = [orjson.loads(x) for x in (MINI / "segments.jsonl").read_bytes().splitlines()]
    got_segs = [orjson.loads(x) for x in (corpus / "segments.jsonl").read_bytes().splitlines()]
    store = ReferenceStore.open(corpus)

    def m(measure: str, /, **p: object) -> list[dict]:
        return store.run_measure(measure, {"window": WINDOW, **p}).as_dicts()

    def by_service(measure: str, /, **p: object) -> dict:
        return {r["service"]: r["n"] for r in m(measure, **p)}

    po = {"service": "checkout", "kind": "SERVER", "name": PLACEORDER, "errors_only": True}
    services = m("services")
    (check,) = json.loads((corpus / "derived/schedule_check.json").read_bytes())["checks"]
    got = {
        "schedule_equal_fixture": (corpus / "schedule.yaml").read_bytes() == (MINI / "schedule.yaml").read_bytes(),
        "schedule_check": {k: check[k] for k in ("status", "onset_ns", "offset_ns", "signature", "affected_records")},
        "frames": {s: t["frames"] for s, t in imported["totals"].items()},
        "records": {s: t["records"] for s, t in imported["totals"].items()},
        "segment_bytes_equal_fixture": [s["sha256"] for s in got_segs] == [s["sha256"] for s in fixture_segs],
        "rows": build["rows"],
        "logs.total": m("log_count")[0]["n"],
        "logs.by_service": by_service("log_count_by_service"),
        "logs.error_by_service": by_service("log_count_by_service", errors_only=True),
        "spans.total": m("span_count")[0]["n"],
        "traces.total": m("distinct_traces")[0]["n"],
        "spans.error_by_service": by_service("span_count_by_service", errors_only=True),
        "metrics.calls_points": m("series_points", metric=CALLS)[0]["n"],
        "metrics.placeorder_error_final": m(
            "series_last",
            metric=CALLS,
            service="checkout",
            attrs=[f"span.name={PLACEORDER}", "status.code=STATUS_CODE_ERROR"],
        )[0]["value_int"],
        "payment_rejections": m("log_count", service="payment", contains="Invalid token")[0],
        "checkout_error_spans": m("span_count", service="checkout", errors_only=True)[0],
        "placeorder_failures": m("span_count", **po)[0]["n"],
        "placeorder_same_cause": m("span_count", **po, message_contains="Invalid token")[0]["n"],
        "proxy_5xx_by_path": {r["url_path"]: r["n"] for r in m("http_errors_by_path", service="frontend-proxy")},
        "services_with_traces": sorted(r["service"] for r in services if r["spans"]),
        "services_with_logs": sorted(r["service"] for r in services if r["logs"]),
    }
    write_artifact(artifacts / "data_path.json", got, sort_keys=True)

    assert got == {
        "schedule_equal_fixture": True,
        "schedule_check": {
            "status": "realised",
            "onset_ns": 1790237682477000000,
            "offset_ns": 1790237747388000000,
            "signature": "Payment request failed. Invalid token. demo.user_context.loyalty_level=gold",
            "affected_records": 15,
        },
        "frames": {"traces": 90, "logs": 89, "metrics": 53},
        "records": {"traces": 24323, "logs": 8394, "metrics": 40712},
        "segment_bytes_equal_fixture": True,
        "rows": {
            "spans": 24323,
            "span_events": got["rows"]["span_events"],
            "logs": 8394,
            "points": 40712,
            "resources": got["rows"]["resources"],
        },
        "logs.total": 8374,
        "logs.by_service": {
            "ad": 47,
            "cart": 464,
            "checkout": 73,
            "currency": 242,
            "email": 17,
            "frontend": 959,
            "frontend-proxy": 3781,
            "load-generator": 890,
            "otelcol-contrib": 1,
            "payment": 44,
            "product-catalog": 1343,
            "quote": 25,
            "recommendation": 420,
            "shipping": 68,
        },
        "logs.error_by_service": {"otelcol-contrib": 1},
        "spans.total": 23888,
        "traces.total": 4051,
        "spans.error_by_service": {
            "checkout": 10,
            "frontend": 10,
            "frontend-proxy": 14,
            "load-generator": 48,
            "payment": 10,
        },
        "metrics.calls_points": 411,
        "metrics.placeorder_error_final": 6,
        "payment_rejections": {"n": 5, "first_ns": 1790237682483000000, "last_ns": 1790237747388000000},
        "checkout_error_spans": {
            "n": 10,
            "first_ns": 1790237679739206793,
            "last_ns": got["checkout_error_spans"]["last_ns"],
        },
        "placeorder_failures": 5,
        "placeorder_same_cause": 5,
        "proxy_5xx_by_path": {"/api/checkout": 8, "/api/recommendations": 2},
        "services_with_traces": [
            "ad",
            "cart",
            "checkout",
            "currency",
            "email",
            "frontend",
            "frontend-proxy",
            "frontend-web",
            "image-provider",
            "load-generator",
            "payment",
            "product-catalog",
            "recommendation",
            "shipping",
            "telemetry-docs",
        ],
        "services_with_logs": [
            "ad",
            "cart",
            "checkout",
            "currency",
            "email",
            "frontend",
            "frontend-proxy",
            "load-generator",
            "otelcol-contrib",
            "payment",
            "product-catalog",
            "quote",
            "recommendation",
            "shipping",
        ],
    }
