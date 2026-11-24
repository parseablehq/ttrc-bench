"""Data-equivalence gate (U6): does every stack hold the data the session sent?

Two layers, both exact:
1. Completeness, from the ingest ledger (otb/ingest.py): every selected frame acknowledged, none rejected, none
   later than the stack's per-signal bound; plus the collector's own refusal counters (DESIGN amendment 1). A
   rejected, missing, in-doubt or late frame fails as its own check over its records' event span, so it excludes
   the instances whose footprint holds that span and not every instance on the signal.
2. Content, sampled: a pre-registered plan (seed) of one-minute windows, every realised fault and decoy window,
   and sampled trace ids, with the check families of the closed Check catalogue (otb/stacks.py) per window.
   Reference values come from otb/ref measures in recording time, restricted to the frames the session sent;
   probes ask each stack in wall time (recording time + the session shift).

Files under state/sessions/<id>/gate/:
  plan.json, expected.json                  the plan and its reference values (per stack where names differ)
  <stack>/responses.jsonl.zst               every HTTP exchange of every probe, raw, last record per point wins
  <stack>/probes.jsonl                      the parsed observation of every probe
  <stack>/ingest.json, counters_start.json  the ledger report and the counter baseline the verdict used
  gate.json                                 judge(...) over the files above: pure, so `otb gate --rejudge` re-derives
                                            it offline by replaying each probe against its recorded responses
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import threading
import time
from bisect import bisect_left
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from itertools import islice
from pathlib import Path
from typing import BinaryIO, Literal

import pyarrow as pa
import yaml
import zstandard

from otb.archive import SIGNALS, Archive
from otb.cli import command
from otb.footprint import Footprint, Region
from otb.ingest import REPO, Session, Target, corpus_path, selected_frames, sessions_dir, wiped_since, write_reports
from otb.ref.store import ReferenceStore
from otb.stacks import (
    Check,
    CountLogs,
    CountSpans,
    DistinctTraces,
    Exact,
    IngestCounters,
    MaxAttrLen,
    MetricNames,
    Probe,
    ProbeError,
    Response,
    SeriesLast,
    SeriesPoints,
    StackContext,
    StackDef,
    TraceSpanCount,
    Unsupported,
    Unverifiable,
    check_id,
    check_signal,
    probe_json,
    registry,
    run_probe,
    stack_metric_name,
)
from otb.tasks.suite import load_instances
from otb.timebase import NS, EventNs, Window

Status = Literal["pass", "fail", "unverifiable"]
MIN = 60 * NS
# A synchronous collector counts each record it could not deliver twice: once in its receiver's refused counter and
# once in its exporter send-failed counter, whether the backend answered 5xx (U5 check d: 492 log records answered
# 503, each counter rose by 492) or rejected the frame for good (gke-r2-24h: B1's 2929 records answered 400, 5858).
COUNTS_PER_REFUSAL = 2
CHECK_TYPES: dict[str, type] = {
    c.__name__: c
    for c in (
        CountLogs,
        CountSpans,
        TraceSpanCount,
        DistinctTraces,
        SeriesPoints,
        SeriesLast,
        MetricNames,
        MaxAttrLen,
        IngestCounters,
    )
}


def _dumps(obj: object) -> bytes:
    return json.dumps(obj, indent=1, sort_keys=True).encode() + b"\n"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------- checks as data ----------


def check_to_json(c: Check) -> dict:
    fields = {k: getattr(c, k) for k in c.__slots__}  # type: ignore[attr-defined]
    if "attrs" in fields:
        fields["attrs"] = [list(kv) for kv in fields["attrs"]]
    return {"_check": type(c).__name__, **fields}


def check_from_json(d: dict) -> Check:
    fields = {k: v for k, v in d.items() if k != "_check"}
    if "attrs" in fields:
        fields["attrs"] = tuple(tuple(kv) for kv in fields["attrs"])
    return CHECK_TYPES[d["_check"]](**fields)


def family(c: Check) -> str:
    match c:
        case CountLogs(contains=sub) if sub is not None:
            return "logs.contains"
        case CountLogs(error_only=True):
            return "logs.error_by_service"
        case CountLogs(service=None):
            return "logs.count"
        case CountLogs():
            return "logs.by_service"
        case CountSpans(kind=k) if k is not None:
            return "spans.by_kind"
        case CountSpans(errors_only=True):
            return "spans.error_by_service"
        case CountSpans(service=None):
            return "spans.count"
        case CountSpans():
            return "spans.by_service"
        case TraceSpanCount():
            return "traces.span_count"
        case DistinctTraces():
            return "traces.distinct"
        case SeriesPoints():
            return "metrics.series_points"
        case SeriesLast():
            return "metrics.series_last"
        case MetricNames():
            return "metrics.names"
        case MaxAttrLen():
            return "ingest.max_attr_len"
        case IngestCounters():
            return "ingest.counters"
    raise AssertionError(c)


@dataclass(frozen=True, slots=True)
class Point:
    id: str
    family: str
    check: Check
    window: Window | None  # recording time; None for IngestCounters

    @staticmethod
    def of(c: Check, w: Window | None) -> Point:
        where = f"{w.start}-{w.end}" if w else "-"
        return Point(f"{check_id(c)}@{where}", family(c), c, w)

    def to_json(self) -> dict:
        w = None if self.window is None else [self.window.start, self.window.end]
        return {"id": self.id, "family": self.family, "check": check_to_json(self.check), "window": w}

    @staticmethod
    def from_json(d: dict) -> Point:
        w = d["window"]
        return Point(
            d["id"],
            d["family"],
            check_from_json(d["check"]),
            None if w is None else Window(EventNs(w[0]), EventNs(w[1])),
        )


@dataclass(frozen=True, slots=True)
class Plan:
    seed: int
    session: str
    corpus_id: str
    windows: dict[str, list[int]]  # name -> [start, end) recording time
    points: tuple[Point, ...]
    suite: dict | None = None  # {"instances_sha256", "footprints": {instance id: footprint}} the plan covers
    # stack id -> {"live_window_s", "traces": {trace id: span start times}}: traces to fetch by id (see long_traces)
    fetch_by_id: dict[str, dict] | None = None

    def to_json(self) -> bytes:
        return _dumps(
            {
                "seed": self.seed,
                "session": self.session,
                "corpus_id": self.corpus_id,
                "windows": self.windows,
                "points": [p.to_json() for p in self.points],
                "suite": self.suite,
                "fetch_by_id": self.fetch_by_id,
            }
        )

    @staticmethod
    def load(path: Path) -> Plan:
        d = json.loads(path.read_text())
        pts = tuple(Point.from_json(p) for p in d["points"])
        return Plan(d["seed"], d["session"], d["corpus_id"], d["windows"], pts, d.get("suite"), d.get("fetch_by_id"))

    def by_id_traces(self, stack: str) -> dict[str, list[int]]:
        return ((self.fetch_by_id or {}).get(stack) or {}).get("traces", {})


# ---------- the plan: pure given the reference store, the schedule and the session ----------


def _minutes(w: Window) -> list[int]:
    first = -(-w.start // MIN) * MIN
    return list(range(first, w.end - MIN + 1, MIN))


def schedule_windows(schedule: dict, scope: Window, cap_min: int = 15) -> dict[str, Window]:
    """Every realised fault and decoy window, widened to whole minutes, clipped to the scope, capped in length."""
    out = {}
    for e in list(schedule.get("entries") or []) + list(schedule.get("unplanned") or []):
        if e.get("status") != "realised" or not e.get("window"):
            continue
        start = max(e["window"]["start"] // MIN * MIN, -(-scope.start // MIN) * MIN)
        end = min(-(-e["window"]["end"] // MIN) * MIN, scope.end // MIN * MIN, start + cap_min * MIN)
        if end > start:
            out[f"{e['kind']}:{e['slot_id']}"] = Window(EventNs(start), EventNs(end))
    return out


def _numeric(v: str) -> bool:
    try:
        float(v)
    except ValueError:
        return False
    return True


def _series_rows(store: ReferenceStore, w: Window) -> list[tuple]:
    """Every series with a point in the window. A series is named by its service and every point attribute;
    series with an attribute value that looks numeric are left out because stacks type them differently
    (Parseable flattens them into typed columns). A name that several resources share (the background's pods
    emit the same metric for the same service) is left out too: its points tie on timestamp, so "the last
    point" is not one value, and the reference and a backend break the tie differently."""
    rows = store.con.execute(
        """
        SELECT metric, coalesce(unit, '') AS unit, type, coalesce(monotonic, false) AS monotonic, service,
               map_keys(attrs) AS k, map_values(attrs) AS v, count(*) AS n
        FROM points
        WHERE ts_ns >= ? AND ts_ns < ? AND service IS NOT NULL
        GROUP BY metric, unit, type, monotonic, service, k, v
        HAVING count(DISTINCT resource_id) = 1
        ORDER BY metric, unit, type, monotonic, service, CAST(k AS VARCHAR), CAST(v AS VARCHAR)
        """,
        [w.start, w.end],
    ).fetchall()
    return [r for r in rows if not any(_numeric(v) for v in r[6])]


def _series(row: tuple) -> tuple[str, str, str, bool, tuple]:
    m, unit, typ, mono, svc, ks, vs, _ = row
    return (
        m,
        unit,
        typ,
        mono,
        (("service.name", svc), *sorted(kv for kv in zip(ks, vs, strict=True) if kv[0] != "service.name")),
    )


def _pick_series(store: ReferenceStore, w: Window, rng: random.Random) -> list[tuple[str, str, str, bool, tuple]]:
    """One monotonic sum, one gauge and one histogram series in the window, seeded."""
    rows = _series_rows(store, w)
    kinds: list[Callable[[tuple], bool]] = [
        lambda r: r[2] == "sum" and r[3],
        lambda r: r[2] == "gauge",
        lambda r: r[2] == "histogram",
    ]
    return [_series(rng.choice(cands)) for want in kinds if (cands := [r for r in rows if want(r)])]


def _longest_key(store: ReferenceStore, signal: str, w: Window) -> str | None:
    """The attribute with the longest string value. Map and array values are skipped: the reference stores them
    as canonical JSON, while each stack renders them its own way, so their length is not a fact about the data."""
    table, ts = ("spans", "start_ns") if signal == "traces" else ("logs", "ts_ns")
    row = store.con.execute(
        f"""
        SELECT k, max(length(v)) AS n FROM (
            SELECT unnest(map_keys(attrs)) AS k, unnest(map_values(attrs)) AS v FROM {table}
            WHERE {ts} >= ? AND {ts} < ?)
        WHERE NOT (starts_with(v, '{{') OR starts_with(v, '['))
        GROUP BY k ORDER BY n DESC, k LIMIT 1
        """,
        [w.start, w.end],
    ).fetchone()
    return row[0] if row else None


def window_checks(store: ReferenceStore, w: Window, rng: random.Random, *, attr_lengths: bool) -> list[Check]:
    svc = store.run_measure("services", {"window": w}).as_dicts()
    log_svcs = [r["service"] for r in svc if r["logs"]]
    span_svcs = [r["service"] for r in svc if r["spans"]]
    err_logs = store.run_measure("log_count_by_service", {"window": w, "errors_only": True}).as_dicts()
    err_spans = store.run_measure("span_count_by_service", {"window": w, "errors_only": True}).as_dicts()
    checks: list[Check] = [CountLogs(None, False), CountLogs(None, True)]
    checks += [CountLogs(s, False) for s in log_svcs]
    checks += [CountLogs(r["service"], True) for r in err_logs if r["n"] and r["service"] is not None]
    checks += [CountSpans(None, False), CountSpans(None, True), CountSpans(None, False, "SERVER")]
    checks += [CountSpans(None, False, "CLIENT")]
    checks += [CountSpans(s, False) for s in span_svcs]
    checks += [CountSpans(r["service"], True) for r in err_spans if r["n"] and r["service"] is not None]
    top = sorted((r for r in svc if r["spans"]), key=lambda r: (-r["spans"], r["service"]))[:3]
    checks += [DistinctTraces(None)] + [DistinctTraces(r["service"]) for r in top]
    checks += [MetricNames()]
    for m, unit, typ, mono, attrs in _pick_series(store, w, rng):
        checks.append(SeriesPoints(m, attrs, unit, typ, mono))  # type: ignore[arg-type]
        if typ != "histogram":
            checks.append(SeriesLast(m, attrs, unit, typ, mono))  # type: ignore[arg-type]
    if attr_lengths:
        for sig in ("traces", "logs"):
            key = _longest_key(store, sig, w)
            if key is not None:
                checks.append(MaxAttrLen(sig, key))  # type: ignore[arg-type]
    return checks


def instance_checks(store: ReferenceStore, fp: Footprint, scope: Window, rng: random.Random) -> list[Point]:
    """Direct checks of every region of an instance's footprint, over the region's own window: sampled minutes
    elsewhere say nothing about it. Every service is counted one by one, so a failure names its service; a
    named metric is checked by one seeded series per service; a trace read by id is checked by id."""
    points: list[Point] = []
    for r in fp.regions:
        w = r.window
        assert w is not None, "instance regions are windowed"
        services: list[str | None] = sorted(r.services) if r.services else []
        if r.signal == "metrics":
            if r.metrics is None:
                points.append(Point.of(MetricNames(), w))
                continue
            rows = _series_rows(store, w)
            for m in sorted(r.metrics):
                for s in services or [None]:
                    cands = [x for x in rows if x[0] == m and s in (None, x[4])]
                    if not cands:
                        points.append(Point.of(MetricNames(), w))
                        continue
                    _, unit, typ, mono, attrs = _series(rng.choice(cands))
                    points.append(Point.of(SeriesPoints(m, attrs, unit, typ, mono), w))  # type: ignore[arg-type]
                    if typ != "histogram":
                        points.append(Point.of(SeriesLast(m, attrs, unit, typ, mono), w))  # type: ignore[arg-type]
            continue
        if not services:
            col = "logs" if r.signal == "logs" else "spans"
            services = [x["service"] for x in store.run_measure("services", {"window": w}).as_dicts() if x[col]]
        count = CountLogs if r.signal == "logs" else CountSpans
        points += [Point.of(count(s, False), w) for s in services or [None]]  # type: ignore[operator]
    return points + [Point.of(TraceSpanCount(t), scope) for t in fp.trace_ids]


def is_trace_search(p: Point) -> bool:
    """A search over the spans stored in a window: what a trace fetched by id changes (B1's _with_by_id)."""
    return (
        p.window is not None
        and isinstance(p.check, CountSpans | DistinctTraces | MaxAttrLen)
        and check_signal(p.check) == "traces"
    )


def trace_windows(plan: Plan) -> list[Window]:
    """The windows of the plan's trace searches, merged: the only places a long trace has to be fetched for."""
    merged: list[list[int]] = []
    for a, b in sorted({(p.window.start, p.window.end) for p in plan.points if is_trace_search(p)}):  # type: ignore[union-attr]
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [Window(EventNs(a), EventNs(b)) for a, b in merged]


def frame_dues(archive: Archive) -> Iterator[tuple[str, int, int]]:
    """(seg, frame, due) of every traces frame, from the index files alone. A replay sends a frame when its newest
    record is due, so that is when its spans arrive."""
    for seg in archive.segments("traces"):
        name = f"{seg.signal}-{seg.stream}-{seg.seq:06d}"
        for e in archive.index(seg.signal, seg.stream, seg.seq):
            yield name, e.index, e.max_event_ns


DUE_CHUNK = 500_000  # frame dues loaded into DuckDB per Arrow batch: bounds the Python-side rows in flight


def long_traces(
    store: ReferenceStore, dues: Iterable[tuple[str, int, int]], live_window_s: float, windows: Iterable[Window]
) -> dict[str, list[int]]:
    """Traces with a span starting in one of `windows` whose spans arrive spread over more than live_window_s
    (the spread of the due times of the frames that carry them), with the start times of all their spans. The
    aggregation runs in DuckDB under the store's memory limit; no spans row reaches Python."""
    con = store.con
    windows = list(windows)
    con.execute("CREATE OR REPLACE TEMP TABLE frame_due (seg VARCHAR, frame INTEGER, due_ns BIGINT)")
    it = iter(dues)
    while chunk := list(islice(it, DUE_CHUNK)):
        batch = pa.table(
            {"seg": [r[0] for r in chunk], "frame": [r[1] for r in chunk], "due_ns": [r[2] for r in chunk]}
        )
        con.register("frame_due_chunk", batch)
        con.execute("INSERT INTO frame_due SELECT seg, CAST(frame AS INTEGER), due_ns FROM frame_due_chunk")
        con.unregister("frame_due_chunk")
    # One row per whole second a window covers, so the candidate scan is an equality (hash) join on the span's
    # second, then the exact bounds: a range join over hundreds of windows runs as a merge join that costs
    # spans x windows.
    buckets = pa.table(
        {
            "sec": [sec for w in windows for sec in range(w.start // NS, -(-w.end // NS))],
            "start_ns": [w.start for w in windows for _ in range(w.start // NS, -(-w.end // NS))],
            "end_ns": [w.end for w in windows for _ in range(w.start // NS, -(-w.end // NS))],
        },
        schema=pa.schema([("sec", pa.int64()), ("start_ns", pa.int64()), ("end_ns", pa.int64())]),
    )
    con.register("gate_buckets", buckets)
    out = con.execute(
        f"""
        WITH candidates AS (
            SELECT DISTINCT s.trace_id FROM spans s JOIN gate_buckets b
            ON s.start_ns // {NS} = b.sec AND s.start_ns >= b.start_ns AND s.start_ns < b.end_ns
        ),
        spread AS (
            SELECT s.trace_id FROM spans s JOIN frame_due f ON s.seg = f.seg AND s.frame = f.frame
            WHERE s.trace_id IN (SELECT trace_id FROM candidates)
            GROUP BY s.trace_id HAVING max(f.due_ns) - min(f.due_ns) > ?
        )
        SELECT trace_id, list(start_ns ORDER BY start_ns) FROM spans
        WHERE trace_id IN (SELECT trace_id FROM spread)
        GROUP BY trace_id ORDER BY trace_id
        """,
        [live_window_s * NS],
    ).fetchall()
    con.execute("DROP TABLE frame_due")
    con.unregister("gate_buckets")
    return dict(out)


def make_plan(
    store: ReferenceStore,
    schedule: dict,
    scope: Window,
    *,
    seed: int,
    session: str,
    corpus_id: str,
    minute_windows: int = 12,
    trace_samples: int = 20,
    suite: tuple[str, Mapping[str, Footprint]] | None = None,
) -> Plan:
    rng = random.Random(seed)
    minutes = _minutes(scope)
    chosen = sorted(rng.sample(minutes, min(minute_windows, len(minutes))))
    windows: dict[str, Window] = {f"minute:{m}": Window(EventNs(m), EventNs(m + MIN)) for m in chosen}
    windows |= schedule_windows(schedule, scope)
    points: list[Point] = [Point.of(IngestCounters(), None)]
    for name, w in windows.items():
        for c in window_checks(store, w, rng, attr_lengths=name.startswith("minute:")):
            points.append(Point.of(c, w))
    traces = store.con.execute(
        f"""
        SELECT trace_id, bool_or(status_code = 'ERROR') AS err FROM spans
        WHERE {" OR ".join(["(start_ns >= ? AND start_ns < ?)"] * len(chosen)) or "false"}
        GROUP BY trace_id ORDER BY trace_id
        """,
        [x for m in chosen for x in (m, m + MIN)],
    ).fetchall()
    errs = [t for t, e in traces if e]
    oks = [t for t, e in traces if not e]
    picked = rng.sample(errs, min(len(errs), trace_samples // 2))
    picked += rng.sample(oks, min(len(oks), trace_samples - len(picked)))
    points += [Point.of(TraceSpanCount(t), scope) for t in sorted(picked)]
    suite_json = None
    if suite is not None:
        sha, footprints = suite
        for iid, fp in sorted(footprints.items()):
            points += instance_checks(store, fp, scope, random.Random(f"{seed}:{iid}"))
            own = sorted({(r.window.start, r.window.end) for r in fp.regions if r.window is not None})
            windows |= {f"instance:{iid}:{k}": Window(EventNs(a), EventNs(b)) for k, (a, b) in enumerate(own)}
        suite_json = {"instances_sha256": sha, "footprints": {i: fp.to_json() for i, fp in sorted(footprints.items())}}
    seen: set[str] = set()
    unique = [p for p in points if not (p.id in seen or seen.add(p.id))]  # type: ignore[func-returns-value]
    named = {k: [w.start, w.end] for k, w in windows.items()}
    return Plan(seed, session, corpus_id, named, tuple(unique), suite_json)


# ---------- reference values ----------


def _service_and_attrs(attrs: tuple[tuple[str, str], ...]) -> tuple[str | None, list[str]]:
    svc = next((v for k, v in attrs if k == "service.name"), None)
    return svc, [f"{k}={v}" for k, v in attrs if k != "service.name"]


def _as_number(v: object) -> object:
    return int(v) if isinstance(v, float) and v.is_integer() else v


def reference_value(store: ReferenceStore, p: Point) -> object:
    """Expected value of one point, in OTel terms. MetricNames returns the metric identities; each stack's names
    are derived from them by stack_metric_name()."""
    c, w = p.check, p.window
    match c:
        case CountLogs(service=s, error_only=e, contains=sub):
            return store.run_measure("log_count", {"window": w, "service": s, "errors_only": e, "contains": sub}).one()[
                "n"
            ]
        case CountSpans(service=s, errors_only=e, kind=k):
            return store.run_measure("span_count", {"window": w, "service": s, "errors_only": e, "kind": k}).one()["n"]
        case TraceSpanCount(trace_id=t):
            return store.run_measure("trace_spans", {"window": w, "trace_id": t}).one()["n"]
        case DistinctTraces(service=s):
            return store.run_measure("distinct_traces", {"window": w, "service": s}).one()["n"]
        case SeriesPoints(metric=m, attrs=attrs):
            svc, kv = _service_and_attrs(attrs)
            return store.run_measure("series_points", {"window": w, "metric": m, "service": svc, "attrs": kv}).one()[
                "n"
            ]
        case SeriesLast(metric=m, attrs=attrs):
            svc, kv = _service_and_attrs(attrs)
            rows = store.run_measure("series_last", {"window": w, "metric": m, "service": svc, "attrs": kv}).as_dicts()
            if not rows:
                return None
            return rows[0]["value_int"] if rows[0]["value_int"] is not None else _as_number(rows[0]["value"])
        case MetricNames():
            rows = store.run_measure("metric_descriptors", {"window": w}).as_dicts()
            return [[r["metric"], r["unit"], r["type"], r["monotonic"]] for r in rows]
        case MaxAttrLen(signal=sig, key=key):
            return store.run_measure("attr_max_len", {"window": w, "signal": sig, "key": key}).one()["n"]
        case IngestCounters():
            return dict.fromkeys(SIGNALS, 0)
    raise AssertionError(c)


def stack_expected(stack: StackDef, p: Point, reference: object) -> object:
    if isinstance(p.check, MetricNames):
        return sorted({stack_metric_name(stack, *d) for d in reference})  # type: ignore[union-attr]
    return reference


# ---------- probing (live) ----------


def probe_from_json(d: dict) -> Probe:
    match d["result"]:
        case "exact":
            return Exact(d["value"], d.get("note"))
        case "unverifiable":
            return Unverifiable(d["reason"], d.get("approximate"))
        case "unsupported":
            return Unsupported(d["reason"])
        case "error":
            return ProbeError(d["error"])
    raise ValueError(d)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = path.read_bytes()
    return [json.loads(line) for line in data[: data.rfind(b"\n") + 1].splitlines() if line]


def _last_by_id(path: Path) -> dict[str, dict]:
    return {r["id"]: r for r in _read_jsonl(path)}


RESPONSES = "responses.jsonl.zst"
FETCH = "fetch:"  # response id of a trace fetched by id, outside the plan's points


def append_response(f: BinaryIO, record: dict) -> None:
    """One independent zstd frame per record: appendable, and a frame torn by a kill is dropped on read. Tempo
    span searches return every matched span, so raw responses run to gigabytes per stack uncompressed."""
    f.write(zstandard.ZstdCompressor(level=3).compress(json.dumps(record).encode() + b"\n"))
    f.flush()


def read_responses(path: Path) -> Iterator[dict]:
    """Recorded probe responses in order, streamed. A torn trailing frame (a killed gate) ends the stream."""
    if not path.exists():
        return
    with path.open("rb") as f, zstandard.ZstdDecompressor().stream_reader(f, read_across_frames=True) as r:
        buf = b""
        while True:
            try:
                chunk = r.read(1 << 20)
            except zstandard.ZstdError:
                return
            if not chunk:
                return
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for line in lines:
                if line:
                    yield json.loads(line)


def arrival_horizon(session: Session, ingest: Mapping) -> int | None:
    """How far past a window's end an arrival-time store (B4) is queried: the ingest report's query_horizon_s, the
    longest event-time span of a frame plus the lateness it was sent with, so rows a stalled stack took in late
    are still inside the queried range. A report written before that was measured gives the span alone: a replay
    session's, recorded when it started; a follow session's, derived from the frames the stack was sent, since a
    live capture's frames arrive after the session starts."""
    if ingest.get("query_horizon_s") is not None:
        return ingest["query_horizon_s"]
    return legacy_horizon(session, ingest)


def legacy_horizon(session: Session, ingest: Mapping) -> int | None:
    """The horizon probes recorded before each response carried its own (`horizon_s`) ran under, so those replay
    with the bodies they sent even after a rerun rewrote the ingest report with a measured horizon."""
    return session.arrival_horizon_s if session.mode == "replay" else ingest.get("arrival_horizon_s")


def recorded_horizon(rec: Mapping, session: Session, ingest: Mapping) -> int | None:
    return rec["horizon_s"] if "horizon_s" in rec else legacy_horizon(session, ingest)


def probe_stack(
    stack: StackDef, plan: Plan, session: Session, out: Path, log: Callable[[str], None], repo: Path = REPO
) -> None:
    """Sequential probes of one stack, appended as they complete. Resume skips points whose last observation is
    Exact, Unverifiable or Unsupported and retries ProbeError (amendment 3).

    First the traces the plan fetches by id for this stack, once each (a recorded fetch is replayed, not
    repeated). A point those traces change, observed before they were fetched, is re-derived from its recorded
    responses, so probes.jsonl stays what a rejudge replays."""
    out.mkdir(parents=True, exist_ok=True)
    last = _last_by_id(out / "probes.jsonl")
    done = {i for i, r in last.items() if r["result"] != "error"}
    by_id = by_id_points(plan, stack.id)
    stale = {pid for pid in by_id if pid in done and not last[pid].get("by_id")}
    todo = [p for p in plan.points if p.id not in done]
    log(f"{stack.id}: {len(todo)} of {len(plan.points)} points to probe, {len(stale)} to re-derive with by-id traces")
    ingest = json.loads((out / "ingest.json").read_text())
    horizon = arrival_horizon(session, ingest)
    ctx = StackContext(stack, repo, arrival_horizon_s=horizon)
    with (out / RESPONSES).open("ab") as resp_f, (out / "probes.jsonl").open("a") as probe_f:

        def write(p: Point, got: Probe, at: str, extra: dict) -> None:
            line = {"id": p.id, "at_utc": at} | extra | probe_json(got)
            if p.id in by_id:
                line["by_id"] = True
            probe_f.write(json.dumps(line) + "\n")
            probe_f.flush()

        ctx.by_id = recorded_fetches(stack, plan, out, session, ingest)
        for tid in plan.by_id_traces(stack.id):
            if tid not in ctx.by_id:
                ctx.exchanges.clear()
                spans = stack.probes.fetch_trace(ctx, tid)  # type: ignore[attr-defined]
                record = {"id": FETCH + tid, "at_utc": _now(), "horizon_s": horizon, "exchanges": ctx.exchanges}
                append_response(resp_f, record)
                ctx.by_id[tid] = spans or []
                log(f"{stack.id}: fetched trace {tid} by id, {len(ctx.by_id[tid])} spans")
        points = {p.id: p for p in plan.points if p.id in stale}
        for rec in last_records(out / RESPONSES, set(points)):
            p = points[rec["id"]]
            wall = session.shift.wall_window(p.window) if p.window is not None else None
            replay = ReplayContext(stack, rec["exchanges"], ctx.by_id, recorded_horizon(rec, session, ingest))
            got = run_probe(stack, p.check, wall, replay)  # type: ignore[arg-type]
            write(p, got, rec["at_utc"], {"rederived_utc": _now()})
        for i, p in enumerate(todo):
            ctx.exchanges.clear()
            t0 = time.monotonic()
            wall = session.shift.wall_window(p.window) if p.window is not None else None
            got = run_probe(stack, p.check, wall, ctx)  # type: ignore[arg-type]
            at = _now()
            append_response(resp_f, {"id": p.id, "at_utc": at, "horizon_s": horizon, "exchanges": ctx.exchanges})
            write(p, got, at, {"secs": round(time.monotonic() - t0, 3)})
            if (i + 1) % 100 == 0:
                log(f"{stack.id}: {i + 1}/{len(todo)} probed")
    log(f"{stack.id}: probing done")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def by_id_points(plan: Plan, stack_id: str) -> frozenset[str]:
    """Ids of the trace searches over a window that holds a span start of a trace the stack fetches by id."""
    starts = sorted(t for s in plan.by_id_traces(stack_id).values() for t in s)
    out = set()
    for p in plan.points:
        if starts and is_trace_search(p):
            i = bisect_left(starts, p.window.start)  # type: ignore[union-attr]
            if i < len(starts) and starts[i] < p.window.end:  # type: ignore[union-attr]
                out.add(p.id)
    return frozenset(out)


def request_sha256(body: object | bytes | None) -> str:
    """The hash of a request body as StackContext.http records it (JSON-encoded unless bytes, decoded as text)."""
    data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
    return _sha((data.decode(errors="replace") if data else "").encode())


class ReplayContext(StackContext):
    """A StackContext whose HTTP client answers from recorded exchanges, in order: the probe code re-parses the
    raw responses offline. A request that differs from the recording in method, logical address (the endpoint key
    behind the URL's origin, so a 127.0.0.1 recording replays under the cluster runtime) or body is a replay
    error, not a silent reuse."""

    def __init__(
        self,
        stack: StackDef,
        exchanges: list[dict],
        by_id: dict[str, list[dict]] | None = None,
        arrival_horizon_s: int | None = None,
    ) -> None:
        super().__init__(stack, arrival_horizon_s=arrival_horizon_s)
        self._recorded = list(exchanges)
        self.by_id = by_id or {}

    def secret(self, name: str) -> str:
        return "replayed"

    def has_secret(self, name: str) -> bool:
        return True

    def http(self, method: str, url: str, *, body: object | bytes | None = None, **_: object) -> Response:  # type: ignore[override]
        if not self._recorded:
            raise RuntimeError(f"replay: no recorded exchange for {method} {url}")
        ex = self._recorded.pop(0)
        if ex["method"] != method or not self.stack.same_address(ex["url"], url):
            raise RuntimeError(f"replay: recorded {ex['method']} {ex['url']}, probe asked {method} {url}")
        recorded, asked = _sha((ex.get("request") or "").encode()), request_sha256(body)
        if recorded != asked:
            raise RuntimeError(f"replay: recorded body sha256 {recorded[:12]}, probe sent {asked[:12]} to {url}")
        if "error" in ex:
            raise type(ex["error"]["type"], (OSError,), {})(ex["error"]["message"])
        return Response(ex["status"], ex["response"].encode())


def last_records(path: Path, ids: set[str]) -> Iterator[dict]:
    """The last recorded response of each of `ids`, in two streaming passes, so memory holds one record at a
    time: which record of each id is the last, then exactly those."""
    last = {r["id"]: i for i, r in enumerate(read_responses(path)) if r["id"] in ids}
    for i, rec in enumerate(read_responses(path)):
        if last.get(rec["id"]) == i:
            yield rec


def recorded_fetches(
    stack: StackDef, plan: Plan, stack_dir: Path, session: Session, ingest: Mapping
) -> dict[str, list[dict]]:
    """trace id -> spans, from the recorded fetches by id of the traces the plan names for this stack."""
    wanted = {FETCH + t for t in plan.by_id_traces(stack.id)}
    if not wanted:
        return {}
    out = {}
    for rec in last_records(stack_dir / RESPONSES, wanted):
        tid = rec["id"].removeprefix(FETCH)
        replay = ReplayContext(stack, rec["exchanges"], arrival_horizon_s=recorded_horizon(rec, session, ingest))
        out[tid] = stack.probes.fetch_trace(replay, tid) or []  # type: ignore[attr-defined]
    return out


def replay_observations(stack: StackDef, plan: Plan, session: Session, gate_dir: Path) -> dict[str, Probe]:
    points = {p.id: p for p in plan.points}
    ingest = json.loads((gate_dir / stack.id / "ingest.json").read_text())
    by_id = recorded_fetches(stack, plan, gate_dir / stack.id, session, ingest)
    out: dict[str, Probe] = {}
    for rec in last_records(gate_dir / stack.id / RESPONSES, set(points)):
        p = points[rec["id"]]
        wall = session.shift.wall_window(p.window) if p.window is not None else None
        replay = ReplayContext(stack, rec["exchanges"], by_id, recorded_horizon(rec, session, ingest))
        out[p.id] = run_probe(stack, p.check, wall, replay)  # type: ignore[arg-type]
    return {pid: out[pid] for pid in points if pid in out}


# ---------- the verdict: pure ----------


def _status(statuses: list[str]) -> Status:
    if "fail" in statuses:
        return "fail"
    return "unverifiable" if "unverifiable" in statuses else "pass"


def signal_status(checks: list[dict]) -> Status:
    """A signal fails when any of its checks fails. It is unverifiable when none of its checks of stored data (the
    windowed ones; ledger and counter checks read none) passed: then nothing vouches for its content."""
    if any(c["status"] == "fail" for c in checks):
        return "fail"
    return (
        "pass" if any(c["status"] == "pass" and c["region"]["window"] is not None for c in checks) else "unverifiable"
    )


def check_region(c: Check, w: Window | None) -> Region:
    """Where in the data a check vouches: its signal, service (None: every service), window and metric."""
    service, metric = None, None
    match c:
        case CountLogs(service=s) | CountSpans(service=s) | DistinctTraces(service=s):
            service = s
        case SeriesPoints(metric=m, attrs=a) | SeriesLast(metric=m, attrs=a):
            service, metric = dict(a).get("service.name"), m
    sig = check_signal(c)
    assert sig is not None
    return Region(
        sig,
        None if service is None else frozenset({service}),
        w,
        None if metric is None else frozenset({metric}),
    )


def localise(plan: Plan, checks: dict[str, dict]) -> None:
    """Narrow a failed count over every service to the services whose own counts failed, when the plan counts
    every service in that window and the per-service counts add up to the total on both sides: then the stack
    holds nothing outside those services, and the whole difference is theirs."""
    groups: dict[tuple, dict[str | None, str]] = {}
    for p in plan.points:
        match p.check:
            case CountLogs(service=s, error_only=e, contains=None):
                groups.setdefault(("logs", e, None, p.window), {})[s] = p.id
            case CountSpans(service=s, errors_only=e, kind=k):
                groups.setdefault(("traces", e, k, p.window), {})[s] = p.id
    for members in groups.values():
        total = checks.get(members.get(None, ""))
        parts = [checks[i] for s, i in members.items() if s is not None]
        if total is None or total["status"] != "fail" or not parts:
            continue
        values = [(c["expected"], c["got"]) for c in [total, *parts]]
        if not all(type(e) is int and type(g) is int for e, g in values):
            continue
        if sum(e for e, _ in values[1:]) != values[0][0] or sum(g for _, g in values[1:]) != values[0][1]:
            continue
        failed = sorted(s for s, i in members.items() if s is not None and checks[i]["status"] != "pass")
        total["region"]["services"] = failed
        total["localised_to"] = failed


def same_value(got: object, want: object) -> bool:
    """Exact for counts and maps; floats within a relative 1e-12, because stores print doubles with 15 to 17
    significant digits (1252.328920819 and 1252.3289208190001 are the same double read back differently)."""
    numbers = isinstance(got, int | float) and isinstance(want, int | float)
    if numbers and (isinstance(got, float) or isinstance(want, float)):
        return abs(got - want) <= 1e-12 * max(abs(got), abs(want), 1.0)
    return got == want


def late_frame_checks(signal: str, bound_s: float, spans: list[dict]) -> dict[str, dict]:
    """One failed check per late frame, windowed to the event span its records can be stored at: an arrival-time
    store dates them up to the frame's lateness after its newest record. Only tasks whose window holds that span
    are excluded, not every task on the signal."""
    out = {}
    for sp in spans:
        window = Window(EventNs(sp["start_ns"]), EventNs(max(sp["end_ns"], sp["start_ns"] + 1)))
        out[f"ingest.late:{sp['frame']}"] = {
            "family": "ingest.late",
            "signal": signal,
            "expected": f"at most {bound_s} s after event time",
            "got": f"{sp['late_s']} s",
            "status": "fail",
            "reason": f"frame {sp['frame']} arrived {sp['late_s']} s after its newest record",
            "region": Region(signal, window=window).to_json(),
        }
    return out


def frame_checks(kind: str, signal: str, spans: list[dict], ingest: dict) -> dict[str, dict]:
    """One failed check per rejected, missing or in-doubt frame, windowed to its records' event span: a rejected or
    missing frame's records are not in the stack, an in-doubt frame's may be there twice. Only tasks whose window
    holds that span are excluded, not every task on the signal."""
    http = {x["frame"]: x["http"] for x in ingest["rejected"]}
    out = {}
    for sp in spans:
        frame = sp["frame"]
        match kind:
            case "rejected":
                expected, got, why = "accepted", f"HTTP {http.get(frame)}", f"frame {frame} was rejected"
            case "missing":
                expected, got, why = "an outcome", "none", f"frame {frame} has no outcome in the ledger"
            case _:
                expected, got = "stored once", "in doubt"
                why = f"frame {frame} was resent after a feeder died and may be stored twice"
        out[f"ingest.{kind}:{frame}"] = {
            "family": f"ingest.{kind}",
            "signal": signal,
            "expected": expected,
            "got": got,
            "status": "fail",
            "reason": why,
            "region": Region(signal, window=Window(EventNs(sp["start_ns"]), EventNs(sp["end_ns"]))).to_json(),
        }
    return out


def judge_stack(
    plan: Plan,
    expected: Mapping[str, object],
    observations: Mapping[str, Probe],
    ingest: dict,
    counters_start: dict[str, int],
) -> dict:
    checks: dict[str, dict] = {}
    for p in plan.points:
        want = expected[p.id]
        got = observations.get(p.id)
        sig = check_signal(p.check)
        entry: dict[str, object] = {"family": p.family, "signal": sig, "expected": want}
        if sig is not None:
            entry["region"] = check_region(p.check, p.window).to_json()
        match got:
            case None:
                entry |= {"status": "fail", "got": None, "reason": "not probed"}
            case Exact(value=v) if isinstance(p.check, IngestCounters):
                for s in SIGNALS:
                    delta = int(v.get(s, 0)) - int(counters_start.get(s, 0))  # type: ignore[union-attr]
                    refused = ingest["signals"][s]["refused_records"]
                    rejected = ingest["signals"][s].get("rejected_records", 0)  # absent from reports before it
                    expected_delta = COUNTS_PER_REFUSAL * (refused + rejected)
                    accounted = f"{refused} records answered 5xx, then resent" + (
                        f", {rejected} rejected" if rejected else ""
                    )
                    status, reason = "pass", None
                    if delta != expected_delta:
                        status = "fail"
                        reason = (
                            f"{delta} refused or failed records counted since the session started; the ledger "
                            f"accounts for {expected_delta} ({accounted})"
                        )
                    checks[f"{p.id}:{s}"] = {
                        "family": p.family,
                        "signal": s,
                        "expected": expected_delta,
                        "got": delta,
                        "status": status,
                        "reason": reason,
                        "region": Region(s).to_json(),
                    }
                continue
            case Exact(value=v, note=note):
                ok = same_value(v, want)
                entry |= {"status": "pass" if ok else "fail", "got": v, "reason": None if ok else "differs"}
                if note:
                    entry["search_incomplete"] = note
            case Unverifiable(reason=r, approximate=a):
                entry |= {"status": "unverifiable", "got": a, "reason": r}
            case Unsupported(reason=r):
                entry |= {"status": "unverifiable", "got": None, "reason": f"unsupported: {r}"}
            case ProbeError(error=e):
                entry |= {"status": "fail", "got": None, "reason": f"probe error: {e}"}
        checks[p.id] = entry
    reasons = []
    frame_spans = ingest.get("frame_spans")  # absent in ingest reports written before frames were localised
    for s in SIGNALS:
        r = ingest["signals"][s]
        bad, localised = [], []
        doubted = [f for f in ingest["in_doubt"] if f.startswith(s + "/")]
        counts = {"missing": r["frames_missing"], "rejected": r["frames_rejected"], "in_doubt": len(doubted)}
        for kind, n in counts.items():
            spans = (frame_spans or {}).get(kind, {}).get(s)
            if n and spans is not None and len(spans) == n:
                checks |= frame_checks(kind, s, spans, ingest)
                localised.append(f"{n} {kind.replace('_', ' ')}")
            elif n and kind == "missing":
                bad.append(f"{n} frames without an outcome")
            elif n and kind == "rejected":
                names = [x["frame"] for x in ingest["rejected"] if x["frame"].startswith(s + "/")]
                bad.append(f"{n} frames rejected: {', '.join(names[:5])}")
            elif n and frame_spans is not None:
                bad.append(f"{n} frames in doubt (resent after a feeder died): {', '.join(doubted[:5])}")
        spans = ingest.get("late_spans", {}).get(s)
        if r["late_frames"] and (spans is None or len(spans) != r["late_frames"]):
            bad.append(
                f"{r['late_frames']} frames later than {r['late_bound_s']} s: {', '.join(ingest['late'][s][:5])}"
            )
        elif r["late_frames"]:
            checks |= late_frame_checks(s, r["late_bound_s"], spans)
        if r.get("time_strings_left"):  # absent in ingest reports written before otb.timeshift
            bad.append(f"{r['time_strings_left']} recording-time strings left in sent bodies")
        checks[f"ingest.ledger:{s}"] = {
            "family": "ingest.ledger",
            "signal": s,
            "expected": r["frames_expected"],
            "got": r["frames_ok"],
            "status": "fail" if bad else "pass",
            "reason": "; ".join(bad) or (f"{', '.join(localised)} frames failed one by one" if localised else None),
            "region": Region(s).to_json(),
        }
        reasons += [f"{s}: {b}" for b in bad]
    if ingest["in_doubt"] and frame_spans is None:
        reasons.append(
            f"{len(ingest['in_doubt'])} frames in doubt (resent after a feeder died): {ingest['in_doubt'][:5]}"
        )
    localise(plan, checks)
    incomplete = [c["search_incomplete"] for c in checks.values() if "search_incomplete" in c]
    families: dict[str, list[str]] = {}
    signals: dict[str, list[dict]] = {s: [] for s in SIGNALS}
    for c in checks.values():
        families.setdefault(c["family"], []).append(c["status"])  # type: ignore[arg-type]
        if c["signal"] is not None:
            signals[c["signal"]].append(c)  # type: ignore[index]
    return {
        "ingest_complete": bool(ingest["complete"]),
        "ingest_reasons": reasons,
        "families": {f: _status(v) for f, v in sorted(families.items())},
        "signals": {s: signal_status(v) for s, v in signals.items()},
        "counts": {k: sum(1 for c in checks.values() if c["status"] == k) for k in ("pass", "fail", "unverifiable")},
        "search_incomplete": {
            "checks": len(incomplete),
            "spans": len({s for n in incomplete for s in n["spans"]}),
            "traces": len({t for n in incomplete for t in n["traces"]}),
        },
        "checks": checks,
    }


def judge(
    plan: Plan,
    plan_sha256: str,
    expected: dict,
    observations: Mapping[str, Mapping[str, Probe]],
    ingest: Mapping[str, dict],
    counters_start: Mapping[str, dict[str, int]],
) -> bytes:
    stacks = {}
    for sid in sorted(observations):
        exp = {p.id: expected["per_stack"].get(sid, {}).get(p.id, expected["reference"][p.id]) for p in plan.points}
        stacks[sid] = judge_stack(plan, exp, observations[sid], ingest[sid], counters_start.get(sid, {}))
    return _dumps(
        {"corpus_id": plan.corpus_id, "session_id": plan.session, "plan_sha256": plan_sha256, "stacks": stacks}
    )


# ---------- orchestration ----------


def gate_dir(session: Session, repo: Path = REPO) -> Path:
    return session.dir(repo) / "gate"


def build_plan(
    session: Session, seed: int, minutes: int, traces: int, suite: Path | None, repo: Path = REPO
) -> tuple[Plan, dict]:
    """The plan from the session's reference, the schedule, the seed, the suite's footprints and, per stack
    whose search is incomplete for long traces, the traces to fetch by id."""
    corpus = corpus_path(session, repo)
    archive = Archive(corpus)
    frames = None
    if session.window is not None:
        frames = [(f"{r.signal}-{r.stream}-{r.seq:06d}", r.index) for r, _ in selected_frames(archive, session.window)]
    store = ReferenceStore.open(corpus, frames=frames)
    scope = session.window or archive.manifest.window
    assert scope is not None
    schedule = yaml.safe_load((corpus / "schedule.yaml").read_text()) if (corpus / "schedule.yaml").exists() else {}
    footprints = None
    if suite is not None:
        raw = (suite / "instances.jsonl").read_bytes()
        insts = load_instances(suite / "instances.jsonl")
        footprints = (_sha(raw), {i: x.footprint for i, x in insts.items()})
    plan = make_plan(
        store,
        schedule,
        scope,
        seed=seed,
        session=session.id,
        corpus_id=session.corpus_id,
        minute_windows=minutes,
        trace_samples=traces,
        suite=footprints,
    )
    defs = registry(repo)
    fetch = {}
    for sid in session.stacks:
        live_s = getattr(defs[sid].probes, "SEARCH_LIVE_WINDOW_S", None)
        if live_s is not None:
            found = long_traces(store, frame_dues(archive), live_s, trace_windows(plan))
            fetch[sid] = {"live_window_s": live_s, "traces": found}
    plan = replace(plan, fetch_by_id=fetch or None)
    reference = {p.id: reference_value(store, p) for p in plan.points}
    return plan, reference


def load_expected(g: Path, plan: Plan, stacks: list[StackDef]) -> dict:
    reference = json.loads((g / "expected.json").read_text())["reference"]
    per_stack = {}
    for s in stacks:
        diff = {}
        for p in plan.points:
            v = stack_expected(s, p, reference[p.id])
            if v != reference[p.id]:
                diff[p.id] = v
        per_stack[s.id] = diff
    return {"reference": reference, "per_stack": per_stack}


def settle(stack: StackDef, ingest: dict, log: Callable[[str], None]) -> None:
    last = ingest.get("last_outcome_ns")
    if last is None:
        return
    wait = last / NS + stack.settle_s - time.time()
    if wait > 0:
        log(f"{stack.id}: settling {wait:.0f} s after its last frame (settle_s {stack.settle_s:g})")
        time.sleep(wait)


def write_plan(g: Path, plan: Plan, reference: dict, log: Callable[[str], None]) -> None:
    """Write the plan and its reference values. A plan already there may only change by its suite: a point kept
    must keep its reference value, and a point may go only when the suite changed, so recorded probes stay valid
    and the sampled part of the plan never moves."""
    if (g / "plan.json").exists():
        old = Plan.load(g / "plan.json")
        if old.seed != plan.seed:
            raise SystemExit(
                f"{g / 'plan.json'} was made with seed {old.seed}; delete gate/ to re-plan with {plan.seed}"
            )
        old_ref = json.loads((g / "expected.json").read_text())["reference"]
        new_ids = {p.id for p in plan.points}
        changed = [p.id for p in old.points if p.id in new_ids and old_ref[p.id] != reference[p.id]]
        dropped = [p.id for p in old.points if p.id not in new_ids]
        same_suite = (old.suite or {}).get("instances_sha256") == (plan.suite or {}).get("instances_sha256")
        if changed or (dropped and same_suite):
            bad = changed + dropped
            raise SystemExit(f"{g / 'plan.json'}: the new plan drops or changes {len(bad)} points, e.g. {bad[:3]}")
        added = len(new_ids - {p.id for p in old.points})
        log(f"plan: {added} points added, {len(dropped)} dropped with the old suite, {len(plan.points)} in all")
    (g / "expected.json").write_bytes(_dumps({"reference": reference}))
    (g / "plan.json").write_bytes(plan.to_json())


def run_gate(
    session: Session,
    stacks: list[StackDef],
    seed: int,
    minutes: int,
    traces: int,
    suite: Path | None,
    repo: Path = REPO,
) -> bytes:
    g = gate_dir(session, repo)
    g.mkdir(parents=True, exist_ok=True)
    plan, reference = build_plan(session, seed, minutes, traces, suite, repo)
    write_plan(g, plan, reference, _log)
    reports = write_reports(session, [Target.of(s) for s in stacks], repo)
    for s in stacks:
        (g / s.id).mkdir(exist_ok=True)
        (g / s.id / "ingest.json").write_bytes(_dumps(reports[s.id].to_json()))
        start = session.dir(repo) / "ingest" / f"{s.id}.counters_start.json"
        (g / s.id / "counters_start.json").write_bytes(start.read_bytes() if start.exists() else _dumps({}))

    def one(s: StackDef) -> None:
        settle(s, reports[s.id].to_json(), _log)
        probe_stack(s, plan, session, g / s.id, _log, repo)

    threads = [threading.Thread(target=one, args=(s,)) for s in stacks]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return write_verdict(session, g, gated_stacks(g, registry(repo)), live=True)


def write_verdict(session: Session, g: Path, stacks: list[StackDef], *, live: bool) -> bytes:
    """Judge from a gate directory. Live: observations from probes.jsonl. Rejudge: re-derived by replaying each
    probe against its recorded responses."""
    plan_bytes = (g / "plan.json").read_bytes()
    plan = Plan.load(g / "plan.json")
    expected = load_expected(g, plan, stacks)
    observations: dict[str, dict[str, Probe]] = {}
    for s in stacks:
        if live:
            observations[s.id] = {i: probe_from_json(r) for i, r in _last_by_id(g / s.id / "probes.jsonl").items()}
        else:
            observations[s.id] = replay_observations(s, plan, session, g)
    ingest = {s.id: json.loads((g / s.id / "ingest.json").read_text()) for s in stacks}
    counters = {s.id: json.loads((g / s.id / "counters_start.json").read_text()) for s in stacks}
    return judge(plan, _sha(plan_bytes), expected, observations, ingest, counters)


def gated_stacks(g: Path, defs: Mapping[str, StackDef]) -> list[StackDef]:
    return [defs[d.name] for d in sorted(g.iterdir()) if d.is_dir() and d.name in defs]


def as_gated(stack: StackDef, g: Path) -> StackDef:
    """The stack with the lateness bounds its gate ran under, which its ingest report records. Probes widen their
    arrival-time range by the bound, so replaying them under a later stack.yaml sends other bodies."""
    signals = json.loads((g / stack.id / "ingest.json").read_text())["signals"]
    return replace(stack, max_late_s={s: signals[s]["late_bound_s"] for s in SIGNALS})


def rejudge(session_dir: Path, repo: Path = REPO) -> tuple[bytes, bytes | None]:
    """(re-derived gate.json, recorded gate.json or None). Needs no live stack and no corpus."""
    session = Session.load(session_dir / "session.json")
    g = session_dir / "gate"
    recorded = (g / "gate.json").read_bytes() if (g / "gate.json").exists() else None
    stacks = [as_gated(s, g) for s in gated_stacks(g, registry(repo))]
    return write_verdict(session, g, stacks, live=False), recorded


def _log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%SZ', time.gmtime())} {msg}", flush=True)


def summary(gate_json: bytes) -> dict:
    d = json.loads(gate_json)
    out = {}
    for sid, v in d["stacks"].items():
        fails = {k: c for k, c in v["checks"].items() if c["status"] == "fail"}
        out[sid] = {
            "signals": v["signals"],
            "counts": v["counts"],
            "families": v["families"],
            "ingest_reasons": v["ingest_reasons"],
            "first_failures": [
                f"{k[:110]}: expected {str(c['expected'])[:60]} got {str(c['got'])[:60]} ({c['reason']})"
                for k, c in list(fails.items())[:8]
            ],
        }
    return out


@command("gate")
def cmd_gate(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="otb gate")
    p.add_argument("--session", help="session id under state/sessions/")
    p.add_argument("--rejudge", type=Path, help="session dir: re-derive gate.json offline from recorded responses")
    p.add_argument("--write", action="store_true", help="with --rejudge: write the re-derived gate.json")
    p.add_argument("--stacks", default=None)
    p.add_argument("--seed", type=int, default=20261002)
    p.add_argument("--minutes", type=int, default=12, help="seeded one-minute windows")
    p.add_argument("--traces", type=int, default=20, help="sampled trace ids")
    p.add_argument("--suite", type=Path, help="suite dir: add a direct check of every instance footprint to the plan")
    a = p.parse_args(argv)
    if a.rejudge:
        got, recorded = rejudge(a.rejudge)
        same = got == recorded
        if a.write:
            (a.rejudge / "gate" / "gate.json").write_bytes(got)
        print(json.dumps({"identical": same, "sha256": _sha(got), "recorded_sha256": recorded and _sha(recorded)}))
        print(json.dumps(summary(got), indent=1))
        return 0 if same or a.write else 1
    if not a.session:
        p.error("--session or --rejudge is required")
    from otb.stacks import select

    session = Session.load(sessions_dir() / a.session / "session.json")
    if wiped := wiped_since(session):
        p.error(
            f"{a.session}: volumes of {', '.join(wiped)} were deleted after this session; it no longer describes them"
        )
    defs = registry()
    stacks = select(defs, a.stacks) if a.stacks else [defs[s] for s in session.stacks]
    out = run_gate(session, stacks, a.seed, a.minutes, a.traces, a.suite)
    (gate_dir(session) / "gate.json").write_bytes(out)
    print(json.dumps(summary(out), indent=1))
    return 0
