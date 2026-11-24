"""Measures on a hand-made corpus whose answers are known by construction (tests/e2e/synthetic.py).

Ways truth can go wrong, each asserted below:
- a re-entrant request (the entry operation nested inside itself through another span) counts as two requests in
  deepest_error_span and self_time_by_service, so a share or a median is computed over a phantom request;
- a tie (two points of one series at one timestamp, two series sharing a timestamp and value) is broken by
  whatever order DuckDB read the rows in, so series_last, series_increase and per_minute_rate change with the
  Parquet file order or the thread count;
- a float sum adds the same terms in a different order and changes in the last bit;
- quantile at q=0 returns null instead of the smallest value;
- any measure, not only those three, gives different rows when its files are read in another order.

Evidence: tests/e2e/artifacts/measures.json.
"""

import random
from collections.abc import Callable
from pathlib import Path

import duckdb
import pytest
from conftest import write_artifact
from synthetic import NS, log, logs, metric, point, span, traces, write_corpus

from otb.ref.build import ref_dir
from otb.ref.schema import TABLES
from otb.ref.store import ReferenceStore, measures
from otb.timebase import EventNs, Window

REPO = Path(__file__).resolve().parents[2]
T = 1790237400 * NS  # 2026-09-24T08:10:00Z, a minute boundary
W = Window(EventNs(T), EventNs(T + 600 * NS))
B = Window(EventNs(T - 600 * NS), EventNs(T))
CHECKOUT = "POST /api/checkout"
SERVER, CLIENT = 2, 3
A, BT, C = "a" * 32, "b" * 32, "c" * 32
MINUTE = 60 * NS


def frames() -> list[tuple[str, dict]]:
    a0 = T + 60 * NS
    b0 = T + 120 * NS
    proxy_line = '[2026-09-24T08:11:00.000Z] "POST /api/checkout HTTP/1.1" 503 - 0 0 1 - "-" "x" "id" "fp" "c"'
    return [
        traces("frontend", span(A, "a1", None, CHECKOUT, SERVER, a0, 1000, attrs={"http.response.status_code": 500})),
        traces("frontend", span(A, "a2", "a1", "POST", CLIENT, a0 + 1_000_000, 900)),
        traces("frontend", span(A, "a3", "a2", CHECKOUT, SERVER, a0 + 2_000_000, 800, attrs={"url.path": "/x"})),
        traces("checkout", span(A, "a4", "a3", "PlaceOrder", SERVER, a0 + 3_000_000, 500, error="charge failed")),
        traces("payment", span(A, "a5", "a4", "Charge", SERVER, a0 + 4_000_000, 200, error="Invalid token")),
        traces("frontend", span(BT, "b1", None, CHECKOUT, SERVER, b0, 300, attrs={"app.k": "vvvv"})),
        traces("checkout", span(BT, "b2", "b1", "PlaceOrder", SERVER, b0 + 1_000_000, 100, error="charge failed")),
        traces("frontend", span(C, "c1", None, CHECKOUT, SERVER, T - 300 * NS, 100)),
        logs("payment", log(a0 + 4_100_000, "Invalid token", 17, A, "a5")),
        logs("payment", log(a0 + 4_200_000, "Invalid token", 17, A, "a5")),
        logs("frontend-proxy", log(T + 60 * NS, proxy_line)),
        logs("cart", log(T + 30 * NS, "order 0a1b2c3d-0000-4000-8000-000000000001 placed")),
        metric("checkout", "calls", "sum", [point(T - 60 * NS, 5, T - 600 * NS, op="x")]),
        metric("checkout", "calls", "sum", [point(T + 60 * NS, 10, T - 600 * NS, op="x")]),
        metric("checkout", "calls", "sum", [point(T + 120 * NS, 20, T - 600 * NS, op="x")]),
        metric("checkout", "calls", "sum", [point(T + 180 * NS, 30, T - 600 * NS, op="x")]),
        metric("checkout", "calls", "sum", [point(T + 180 * NS, 35, T - 600 * NS, op="x")]),
        metric("checkout", "calls", "sum", [point(T + 120 * NS, 0.1, T - 600 * NS, op="y")]),
        metric("checkout", "calls", "sum", [point(T + 180 * NS, 0.3, T - 600 * NS, op="y")]),
        metric("checkout", "calls", "sum", [point(T + 120 * NS, 1e16, T - 600 * NS, op="z")]),
        metric("checkout", "calls", "sum", [point(T + 180 * NS, 1e16 + 60, T - 600 * NS, op="z")]),
        metric("checkout", "gauge_x", "gauge", [point(T + 300 * NS, 7, k="a")]),
        metric("checkout", "gauge_x", "gauge", [point(T + 300 * NS, 7.0, k="b")]),
    ]


PARAMS: dict[str, dict] = {
    "server_latency_quantiles": {"window": W, "q": 0.95, "min_spans": 1},
    "access_log_errors_by_path": {"window": W, "service": "frontend-proxy"},
    "affected_entry_requests": {"window": W, "signature": "Invalid token"},
    "attr_max_len": {"window": W, "signal": "traces", "key": "app.k"},
    "container_metrics": {"window": W, "service": "checkout"},
    "deepest_error_span": {"window": W, "entry_service": "frontend", "entry_op": CHECKOUT},
    "distinct_traces": {"window": W},
    "entry_ops_of_span": {"window": W, "service": "payment", "name": "Charge"},
    "error_ops": {"window": W},
    "error_rate": {"window": W, "service": "checkout"},
    "error_texts": {"window": W},
    "error_rate_ratio_by_service": {"window": W, "baseline": B, "min_errors": 1, "min_ratio": 1.0},
    "error_signatures": {"window": W, "service": "checkout"},
    "error_spans_per_minute": {"window": W},
    "error_spike_by_service": {"window": W, "baseline": B},
    "failing_traces": {"window": W, "signature": "Invalid token"},
    "http_5xx_by_route": {"window": W, "service": "frontend"},
    "http_errors_by_path": {"window": W, "service": "frontend"},
    "log_burst_by_span": {"window": W, "baseline": B, "min_records": 1},
    "log_count": {"window": W},
    "log_count_by_service": {"window": W},
    "log_needle_tokens": {"window": W, "min_n": 1},
    "max_attr_len": {"window": W},
    "metric_descriptors": {"window": W},
    "metric_names": {"window": W},
    "metric_services": {"window": W, "metric": "calls"},
    "parent_chain": {"window": W, "signature": "Invalid token"},
    "per_minute_rate": {"window": W, "metric": "calls"},
    "recovery": {"window": W, "signature": "Invalid token"},
    "resource_peak": {"window": W, "service": "checkout", "metric": "gauge_x", "container_attr": "container.name"},
    "restart_evidence": {"window": W, "service": "checkout"},
    "self_time_by_service": {"window": W, "baseline": B, "entry_service": "frontend", "entry_op": CHECKOUT},
    "series_increase": {"window": W, "metric": "calls", "attrs": ["op=x"]},
    "series_last": {"window": W, "metric": "gauge_x"},
    "series_points": {"window": W, "metric": "calls"},
    "server_ops": {"window": W, "baseline": B, "min_spans": 1},
    "service_version_change": {"window": W, "service": "frontend"},
    "services": {"window": W},
    "services_missing_signal": {"window": W, "present": "traces", "absent": "logs"},
    "slow_spans": {"window": W, "service": "frontend", "min_duration_ms": 100},
    "span_count": {"window": W},
    "span_count_by_service": {"window": W},
    "span_duration_quantile": {"window": W, "service": "frontend", "name": CHECKOUT, "q": 0.0},
    "trace_span_count": {"trace_id": A},
    "trace_spans": {"window": W, "trace_id": A},
    "trace_summary": {"trace_id": A},
}


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return write_corpus(tmp_path_factory.mktemp("measures") / "synthetic", frames())


def reading(corpus: Path, order: Callable[[list[str]], list[str]], threads: int) -> ReferenceStore:
    """The store with every table's Parquet files read in `order`."""
    con = duckdb.connect()
    con.execute(f"SET threads = {threads}")
    for t in TABLES:
        files = order(sorted(str(p) for p in (ref_dir(corpus) / t).glob("*.parquet")))
        con.execute(f"CREATE VIEW {t} AS SELECT * FROM read_parquet({files!r})")
    return ReferenceStore(con)


def rows(store: ReferenceStore, name: str, **extra: object) -> list[dict]:
    return store.run_measure(name, PARAMS[name] | extra).as_dicts()


def test_reentrant_request_counts_once(corpus: Path) -> None:
    store = ReferenceStore.open(corpus)
    assert rows(store, "deepest_error_span") == [
        {"service": "checkout", "operation": "PlaceOrder", "n": 1, "failures": 2, "share": 0.5},
        {"service": "payment", "operation": "Charge", "n": 1, "failures": 2, "share": 0.5},
    ]
    assert rows(store, "self_time_by_service") == [
        {"service": "frontend", "median_ms": 200.0, "base_median_ms": 100.0, "delta_ms": 100.0},
        {"service": "checkout", "median_ms": 100.0, "base_median_ms": None, "delta_ms": None},
        {"service": "payment", "median_ms": 200.0, "base_median_ms": None, "delta_ms": None},
    ]


def test_ties_resolve_to_one_stated_answer(corpus: Path) -> None:
    store = ReferenceStore.open(corpus)
    assert rows(store, "series_last") == [{"ts_ns": T + 300 * NS, "value": 7.0, "value_int": 7}]
    assert rows(store, "series_increase") == [{"n": 30}]
    assert rows(store, "per_minute_rate", attrs=["op=x"]) == [
        {"minute_ns": T + 3 * MINUTE, "rate_per_s": 0.25},
        {"minute_ns": T + 2 * MINUTE, "rate_per_s": 0.1667},
        {"minute_ns": T + 1 * MINUTE, "rate_per_s": 0.0417},
    ]
    assert rows(store, "span_duration_quantile") == [{"n": 3, "p_ms": 300.0}]
    assert rows(store, "span_duration_quantile", q=1.0) == [{"n": 3, "p_ms": 1000.0}]


def test_every_measure_reads_the_same_in_any_file_order(corpus: Path, artifacts: Path) -> None:
    assert set(PARAMS) == set(measures()), "every measure needs params here"
    rng = random.Random(20261002)
    orders = {
        "sorted, 1 thread": (lambda fs: fs, 1),
        "reversed, 8 threads": (lambda fs: fs[::-1], 8),
        **{
            f"shuffle {i}, 8 threads": ((lambda s: lambda fs: random.Random(s).sample(fs, len(fs)))(rng.random()), 8)
            for i in range(4)
        },
    }
    shas: dict[str, dict[str, str]] = {}
    nonempty = []
    for label, (order, threads) in orders.items():
        store = reading(corpus, order, threads)
        for name in sorted(PARAMS):
            got = store.run_measure(name, PARAMS[name])
            shas.setdefault(name, {})[label] = got.sha256
            if label == "sorted, 1 thread" and any(any(v is not None for v in r) for r in got.rows):
                nonempty.append(name)
    differ = {name: by for name, by in shas.items() if len(set(by.values())) > 1}
    write_artifact(
        artifacts / "measures.json",
        {"orders": list(orders), "measures": len(shas), "nonempty": sorted(nonempty), "differ": differ},
    )
    assert differ == {}
    assert len(nonempty) >= 38, sorted(set(PARAMS) - set(nonempty))
