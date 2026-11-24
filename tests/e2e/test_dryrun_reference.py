"""U2 check on the dry-run corpus: v2's truth and run6's gate reference values reproduced by named measures, and
a build killed mid-way converges to the clean build's Parquet bytes.

v2 counted flagd and flagd-ui telemetry, which normaliser n2 drops (interrogate-1 item 1). The values that change
are named in N2_COUNTS and without_flag_services; their otb values were recounted by a stdlib scan of the raw
recording with those services left out (docs/v1/units/F1.md). Every other value must still equal v2's.

Needs corpora/dryrun with segments (`otb corpus import-v2 ... --rotate-mb 4`) and the v2 result files; skipped
without them. Evidence: tests/e2e/artifacts/dryrun_reference.json.
"""

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import write_artifact

from otb.ref.store import ReferenceStore
from otb.timebase import NS, EventNs, Window

REPO = Path(__file__).resolve().parents[2]
CORPUS = REPO / "corpora/dryrun"
V2 = Path(os.environ.get("OTB_V2_ROOT", str(REPO)))
GROUND_TRUTH = V2 / "corpus/dryrun/ground_truth.json"
EQUIVALENCE = V2 / "results/run6/equivalence.json"

pytestmark = pytest.mark.skipif(
    not (CORPUS / "segments").exists() or not GROUND_TRUTH.exists(), reason="dry-run corpus or v2 results not present"
)

CALLS = "traces.span.metrics.calls"
PLACEORDER = "oteldemo.CheckoutService/PlaceOrder"
CHARGE = "oteldemo.PaymentService/Charge"
REJECTION = "Invalid token"
FLAG_SERVICES = ("flagd", "flagd-ui")
N2_COUNTS = {"spans.total": 222636, "traces.total": 38041, "metrics.calls_points": 4713}


def without_flag_services(v: object) -> object:
    if isinstance(v, dict):
        return {k: without_flag_services(x) for k, x in v.items() if k not in FLAG_SERVICES}
    if isinstance(v, list):
        return [x for x in v if x not in FLAG_SERVICES]
    return v


def n2_tasks(gt_tasks: dict) -> dict:
    out = without_flag_services(gt_tasks)
    out["cold-schema-service-coverage"]["services_with_traces"] -= len(FLAG_SERVICES)
    return out


def n2_gate(v2_gate: dict) -> dict:
    return without_flag_services(v2_gate) | N2_COUNTS


def utc(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp()) * NS


def hhmm(ns: int, seconds: bool = False) -> str:
    return datetime.fromtimestamp(ns // NS, UTC).strftime("%H:%M:%S" if seconds else "%H:%M")


def otb(*args: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "otb.cli", *args], cwd=REPO, capture_output=True, text=True, **kw)


def v2_values(store: ReferenceStore, w: Window) -> tuple[dict, dict, dict]:
    m = store.run_measure

    def grouped(name: str, **p) -> dict:
        return {r["service"]: r["n"] for r in m(name, {"window": w, **p}).as_dicts()}

    checkout_err = m("span_count", {"window": w, "service": "checkout", "errors_only": True}).one()
    rej = m("log_count", {"window": w, "service": "payment", "contains": REJECTION}).one()
    po = {"window": w, "service": "checkout", "kind": "SERVER", "name": PLACEORDER, "errors_only": True}
    placeorder = m("span_count", po).one()["n"]
    same_cause = m("span_count", po | {"message_contains": REJECTION}).one()["n"]
    charge = {"window": w, "service": "checkout", "kind": "CLIENT", "name": CHARGE, "errors_only": True}
    failed_charges = m("span_count", charge).one()["n"]
    po_series = {
        "window": w,
        "metric": CALLS,
        "service": "checkout",
        "attrs": [f"span.name={PLACEORDER}", "status.code=STATUS_CODE_ERROR"],
    }
    calls_final = m("series_last", po_series).one()["value_int"]
    proxy = m("http_errors_by_path", {"window": w, "service": "frontend-proxy"}).as_dicts()
    access = m("access_log_errors_by_path", {"window": w, "service": "frontend-proxy"}).as_dicts()
    services = m("services", {"window": w}).as_dicts()
    traced = {s["service"] for s in services if s["spans"]}
    logged = {s["service"] for s in services if s["logs"]}
    quiet = Window(w.start, EventNs(w.start + 20 * 60 * NS))
    first, last = hhmm(rej["first_ns"]), hhmm(rej["last_ns"])
    tasks = {
        "lookup-checkout-error-spans": {
            "error_span_count": checkout_err["n"],
            "first_error_utc": hhmm(checkout_err["first_ns"], seconds=True),
        },
        "summarise-error-logs": {"error_logs_by_service": grouped("log_count_by_service", errors_only=True)},
        "attribution-proxy-5xx": {
            "http_5xx_count": sum(r["n"] for r in proxy),
            "top_path": proxy[0]["url_path"],
            "top_path_count": proxy[0]["n"],
        },
        "correlation-payment-rejection-log": {"service": "payment", "message": REJECTION, "times_logged": rej["n"]},
        "root-cause-from-trace": {
            "root_cause_service": "payment",
            "error": REJECTION,
            "recurring": True,
            "first_seen_utc": first,
            "last_seen_utc": last,
            "placeorder_failures_total": placeorder,
            "placeorder_failures_same_cause": same_cause,
        },
        "root-cause-from-symptom": {
            "root_cause_service": "payment",
            "error": REJECTION,
            "first_seen_utc": first,
            "last_seen_utc": last,
            "placeorder_failures_same_cause": same_cause,
        },
        "cold-schema-service-coverage": {
            "services_with_traces": len(traced),
            "services_with_logs": len(logged),
            "traces_but_no_logs": sorted(traced - logged),
        },
        "negative-control-cart": {
            "incident_occurred": False,
            "error_span_count": m("span_count", {"window": w, "service": "cart", "errors_only": True}).one()["n"],
            "error_log_count": m("log_count", {"window": w, "service": "cart", "errors_only": True}).one()["n"],
        },
        "negative-control-quiet-payment": {
            "incident_occurred": False,
            "rejected_charges": m("log_count", {"window": quiet, "service": "payment", "contains": REJECTION}).one()[
                "n"
            ],
        },
        "lookup-span-metric-errors": {"metric": CALLS, "placeorder_error_calls": calls_final},
        "root-cause-three-signals": {
            "root_cause_service": "payment",
            "error": REJECTION,
            "start_minute_utc": first,
            "end_minute_utc": last,
            "failed_charge_calls": failed_charges,
            "placeorder_error_calls_metric": calls_final,
        },
    }
    alternatives = {
        "attribution-proxy-5xx": [
            {
                "source": "envoy access logs (one line per request)",
                "answer": {
                    "http_5xx_count": sum(r["n"] for r in access),
                    "top_path": access[0]["url_path"],
                    "top_path_count": access[0]["n"],
                },
            }
        ]
    }
    gate = {
        "logs.total": m("log_count", {"window": w}).one()["n"],
        "logs.by_service": grouped("log_count_by_service"),
        "logs.error_by_service": grouped("log_count_by_service", errors_only=True),
        "spans.total": m("span_count", {"window": w}).one()["n"],
        "traces.total": m("distinct_traces", {"window": w}).one()["n"],
        "spans.error_by_service": grouped("span_count_by_service", errors_only=True),
        "metrics.calls_points": m("series_points", {"window": w, "metric": CALLS}).one()["n"],
        "metrics.placeorder_error_final": calls_final,
    }
    return tasks, alternatives, gate


def parquet_shas(corpus: Path) -> dict[str, str]:
    ref = corpus / "derived/ref"
    return {
        str(p.relative_to(ref)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(ref.glob("*/*.parquet"))
    }


def built_lines(corpus: Path) -> int:
    p = corpus / "derived/ref/built.jsonl"
    return p.read_bytes().count(b"\n") if p.exists() else 0


def kill_build_when(corpus: Path, lines: int) -> None:
    proc = subprocess.Popen(
        [sys.executable, "-m", "otb.cli", "ref", "build", str(corpus), "--workers", "4"],
        cwd=REPO,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
    )
    while proc.poll() is None and built_lines(corpus) < lines:
        time.sleep(0.005)
    os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()
    assert proc.returncode == -signal.SIGKILL, "build finished before it could be killed"


def test_dryrun_reference(tmp_path: Path, artifacts: Path) -> None:
    clean = tmp_path / "clean"
    killed = tmp_path / "killed"
    for d in (clean, killed):
        d.mkdir()
        for name in ("manifest.yaml", "segments.jsonl", "scenario.yaml", "schedule.yaml"):
            shutil.copy(CORPUS / name, d / name)
        (d / "segments").symlink_to(CORPUS / "segments")

    r = otb("ref", "build", str(clean), "--workers", "4")
    assert r.returncode == 0, r.stderr
    report = json.loads(r.stdout)
    report.pop("schedule_check")
    kills = [1, 4, 8]
    for n in kills:
        kill_build_when(killed, n)
    resumed = json.loads(otb("ref", "build", str(killed), "--workers", "4").stdout)
    rerun = json.loads(otb("ref", "build", str(clean), "--workers", "4").stdout)

    gt = json.loads(GROUND_TRUTH.read_text())
    eq = json.loads(EQUIVALENCE.read_text())
    w = Window(EventNs(utc(gt["window_utc"][0])), EventNs(utc(gt["window_utc"][1])))
    tasks, alternatives, gate = v2_values(ReferenceStore.open(clean), w)
    want_gate = n2_gate({k: v["expected"] for k, v in eq["backends"]["clickhouse"]["checks"].items()})
    want_tasks = n2_tasks(gt["tasks"])
    clean_shas, killed_shas = parquet_shas(clean), parquet_shas(killed)
    fault = json.loads((clean / "derived/schedule_check.json").read_bytes())["checks"][0]
    result = {
        "schedule_check_f1": {
            k: fault[k]
            for k in (
                "slot_id",
                "status",
                "onset_utc",
                "offset_utc",
                "onset_delay_s",
                "offset_delay_s",
                "signature",
                "affected_records",
            )
        },
        "window": gt["window_utc"],
        "tasks_mismatch": {
            k: {"want": want_tasks[k], "otb": tasks[k]} for k in want_tasks if want_tasks[k] != tasks[k]
        },
        "alternatives_equal": alternatives == gt["alternatives"],
        "gate_mismatch": {k: {"want": want_gate[k], "otb": gate[k]} for k in want_gate if want_gate[k] != gate[k]},
        "tasks_compared": len(gt["tasks"]),
        "gate_checks_compared": len(want_gate),
        "gate_values": gate,
        "clean_build": {k: report[k] for k in ("segments_total", "segments_built", "rows")},
        "kills_after_built_lines": kills,
        "resumed_build": {k: resumed[k] for k in ("segments_built", "segments_skipped")},
        "rerun_on_clean": {k: rerun[k] for k in ("segments_built", "segments_skipped")},
        "parquet_files": len(clean_shas),
        "parquet_identical_after_kills": clean_shas == killed_shas,
    }
    write_artifact(artifacts / "dryrun_reference.json", result)

    assert fault["status"] == "realised" and "Invalid token" in fault["signature"]
    assert abs(fault["onset_delay_s"]) <= 60 and abs(fault["offset_delay_s"]) <= 60
    assert result["tasks_mismatch"] == {}
    assert result["alternatives_equal"]
    assert result["gate_mismatch"] == {}
    assert result["parquet_identical_after_kills"]
    assert rerun["segments_built"] == 0 and rerun["segments_skipped"] == 10
    assert resumed["segments_skipped"] > 0
