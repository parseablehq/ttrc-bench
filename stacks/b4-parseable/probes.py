"""B4 Parseable: gate probes in Parseable SQL (DataFusion), provisioning, emptiness.

Parseable OSS filters its query API on arrival time (p_timestamp). Each probe asks for the arrival window
[start, end + max_late_s + the session's arrival horizon] and adds an event-time predicate on the record's own timestamp
column, so the answer is about event time. A record arrives with its batch, which is sent when the batch's newest
timestamp is due: a span that starts at 08:10:55 in a batch whose newest end time is 08:11:06 arrives after the
08:10 minute closes, and long spans arrive up to their duration late. A batch the stack took in late (a stall,
5xx retries) arrives later still. The horizon covers both: the longest event-time span of one frame plus the
lateness it was sent with, over the frames the stack answered ok (otb.ingest.StackReport.query_horizon_s;
2401 s of span alone on the dry run). Parseable has no lateness bound (max_late_s null), so a late row is correct
data filed later, and the gate must look where it was filed rather than fail the frame.
Timestamp columns are millisecond precision; window bounds are whole minutes, so the comparison is exact.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from otb.stacks import (
    Check,
    CountLogs,
    CountSpans,
    DistinctTraces,
    Exact,
    HttpError,
    IngestCounters,
    MaxAttrLen,
    MetricNames,
    Probe,
    SeriesLast,
    SeriesPoints,
    Signal,
    StackContext,
    TraceSpanCount,
    basic,
    collector_counters,
    metric_key,
)
from otb.timebase import NS, WallWindow

READER_ROLE = "otb-reader"
KEY_NAME = "otb-agent"
STREAM: dict[Signal, str] = {"traces": "otel_traces", "logs": "otel_logs", "metrics": "otel_metrics"}
EVENT_TIME: dict[Signal, str] = {
    "traces": "span_start_time_unix_nano",
    "logs": "COALESCE(time_unix_nano, observed_time_unix_nano)",
    "metrics": "time_unix_nano",
}
KIND = {"SERVER": 2, "CLIENT": 3}
SPAN = "span_trace_id || span_span_id"  # span events are extra rows; a span is a distinct (trace, span) pair
SYNTHETIC_METRIC = "target_info"  # rows Parseable adds per resource; not an ingested OTel metric


def _admin(ctx: StackContext) -> dict[str, str]:
    return basic(ctx.secret("B4_PARSEABLE_USERNAME"), ctx.secret("B4_PARSEABLE_PASSWORD"))


def _iso(ns: int) -> str:
    return datetime.fromtimestamp(ns // NS, UTC).strftime("%Y-%m-%dT%H:%M:%S") + f".{ns % NS // 1_000_000:03d}Z"


def _lit(v: str) -> str:
    return "'" + v.replace("'", "''") + "'"


def _col(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _rows(ctx: StackContext, sql: str, signal: Signal, w: WallWindow) -> list[dict[str, object]]:
    late = ctx.stack.max_late_s[signal] or 0
    if ctx.arrival_horizon_s is None:
        raise ValueError("B4 probes need the session's arrival horizon; a follow-mode session records none")
    where = f"{EVENT_TIME[signal]} >= {_lit(_iso(w.start))} AND {EVENT_TIME[signal]} < {_lit(_iso(w.end))}"
    body = {
        "query": sql.replace("$WHERE", where),
        "startTime": _iso(w.start),
        # Parseable drops the final partial minute of a query's range (ingest defect 8), so the arrival-time
        # range ends on a minute boundary; the event-time predicate keeps the count to the window.
        "endTime": _iso(-(-(w.end + int((late + ctx.arrival_horizon_s) * NS)) // (60 * NS)) * 60 * NS),
    }
    try:
        out = ctx.http_json(
            "POST", f"{ctx.url('parseable')}/api/v1/query", body=body, headers=_admin(ctx), timeout_s=300
        )
    except HttpError as e:
        if "No field named" in str(e):  # the attribute was never ingested into this dataset: no matching rows
            return []
        raise
    assert isinstance(out, list)
    return out


def _one(ctx: StackContext, sql: str, signal: Signal, w: WallWindow) -> object:
    rows = _rows(ctx, sql, signal, w)
    return rows[0].get("v") if rows else None  # Parseable omits NULL columns from a row


def _attr_filter(attrs: tuple[tuple[str, str], ...]) -> str:
    return "".join(f" AND {_col(k)} = {_lit(v)}" for k, v in attrs)


def probe(check: Check, window: WallWindow, ctx: StackContext) -> Probe:
    w = window
    match check:
        case CountLogs(service=svc, error_only=err, contains=sub):
            sql = "SELECT count(*) AS v FROM otel_logs WHERE $WHERE"
            if svc is not None:
                sql += f' AND "service.name" = {_lit(svc)}'
            if err:
                sql += " AND (severity_number >= 17 OR lower(severity_text) IN ('error', 'fatal'))"
            if sub is not None:
                sql += f" AND strpos(body, {_lit(sub)}) > 0"
            return Exact(int(_one(ctx, sql, "logs", w) or 0))
        case CountSpans(service=svc, errors_only=err, kind=kind):
            sql = f"SELECT count(DISTINCT {SPAN}) AS v FROM otel_traces WHERE $WHERE"
            if svc is not None:
                sql += f' AND "service.name" = {_lit(svc)}'
            if err:
                sql += " AND span_status_code = 2"
            if kind is not None:
                sql += f" AND span_kind = {KIND[kind]}"
            return Exact(int(_one(ctx, sql, "traces", w) or 0))
        case TraceSpanCount(trace_id=tid):
            sql = f"SELECT count(DISTINCT {SPAN}) AS v FROM otel_traces WHERE $WHERE"
            sql += f" AND lower(span_trace_id) = {_lit(tid)}"
            return Exact(int(_one(ctx, sql, "traces", w) or 0))
        case DistinctTraces(service=svc):
            sql = "SELECT count(DISTINCT span_trace_id) AS v FROM otel_traces WHERE $WHERE"
            if svc is not None:
                sql += f' AND "service.name" = {_lit(svc)}'
            return Exact(int(_one(ctx, sql, "traces", w) or 0))
        case SeriesPoints(metric=m, attrs=attrs):
            sql = (
                f"SELECT count(*) AS v FROM otel_metrics WHERE $WHERE AND metric_name = {_lit(m)}{_attr_filter(attrs)}"
            )
            return Exact(int(_one(ctx, sql, "metrics", w) or 0))
        case SeriesLast(metric=m, attrs=attrs):
            sql = (
                f"SELECT data_point_value AS v FROM otel_metrics WHERE $WHERE AND metric_name = {_lit(m)}"
                f"{_attr_filter(attrs)} ORDER BY time_unix_nano DESC, data_point_value DESC NULLS LAST LIMIT 1"
            )
            v = _one(ctx, sql, "metrics", w)
            return Exact(None if v is None else (int(v) if float(v).is_integer() else float(v)))  # type: ignore[arg-type]
        case MetricNames():
            rows = _rows(ctx, "SELECT DISTINCT metric_name AS m FROM otel_metrics WHERE $WHERE", "metrics", w)
            return Exact(sorted({metric_key(str(r["m"])) for r in rows if r.get("m") != SYNTHETIC_METRIC}))
        case MaxAttrLen(signal=sig, key=key):
            sql = f"SELECT max(character_length(CAST({_col(key)} AS VARCHAR))) AS v FROM {STREAM[sig]} WHERE $WHERE"
            v = _one(ctx, sql, sig, w)
            return Exact(None if v is None else int(v))  # type: ignore[arg-type]
        case IngestCounters():
            return Exact(dict(collector_counters(ctx)))


def reader_api_key(ctx: StackContext) -> bool:
    """An API key bound to a reader role on the benchmark datasets, for the MCP server. True when it changed."""
    base = ctx.url("parseable")
    if ctx.has_secret("B4_PARSEABLE_MCP_KEY"):
        r = ctx.http("GET", f"{base}/api/v1/logstream", headers={"X-API-Key": ctx.secret("B4_PARSEABLE_MCP_KEY")})
        if r.status == 200:
            return False
    ctx.http_json(
        "PUT",
        f"{base}/api/v1/role/{READER_ROLE}",
        # the benchmark datasets only: on gke the bucket also holds another deployment's datasets
        body=[{"privilege": "reader", "resource": {"stream": s}} for s in STREAM.values()],
        headers=_admin(ctx),
    )
    for key in ctx.http_json("GET", f"{base}/api/prism/v1/apikeys", headers=_admin(ctx)):  # type: ignore[union-attr]
        if key.get("keyName") == KEY_NAME:
            ctx.http_json("DELETE", f"{base}/api/prism/v1/apikeys/{key['keyId']}", headers=_admin(ctx))
    created = ctx.http_json(
        "POST", f"{base}/api/prism/v1/apikeys", body={"keyName": KEY_NAME, "roles": [READER_ROLE]}, headers=_admin(ctx)
    )
    ctx.set_secret("B4_PARSEABLE_MCP_KEY", created["apiKey"])  # type: ignore[index]
    return True


def provisioning() -> Mapping[str, Callable[[StackContext], bool]]:
    return {"reader_api_key": reader_api_key}


def is_empty(ctx: StackContext) -> bool:
    """No benchmark dataset exists yet; other datasets in a shared object store do not count."""
    listed = ctx.http_json("GET", f"{ctx.url('parseable')}/api/v1/logstream", headers=_admin(ctx))
    return not {s["name"] for s in listed} & set(STREAM.values())  # type: ignore[union-attr]
