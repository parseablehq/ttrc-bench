"""B3 ClickHouse: gate probes in ClickHouse SQL over the contrib exporter's otel.* tables, provisioning, emptiness.
Timestamps are DateTime64(9), so window predicates are exact to the nanosecond."""

from __future__ import annotations

from collections.abc import Callable, Mapping

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
    SeriesLast,
    SeriesPoints,
    StackContext,
    TraceSpanCount,
    basic,
    collector_counters,
    metric_key,
)
from otb.timebase import WallWindow

METRIC_TABLES = ("sum", "gauge", "histogram", "exponential_histogram", "summary")
KIND = {"SERVER": "Server", "CLIENT": "Client"}
ERROR_LOG = "(SeverityNumber >= 17 OR lower(SeverityText) IN ('error', 'fatal'))"


def _lit(v: str) -> str:
    return "'" + v.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _query(ctx: StackContext, sql: str, user: str = "default", password_secret: str = "B3_CH_ADMIN_PASSWORD") -> list:
    out = ctx.http_json(
        "POST",
        f"{ctx.url('clickhouse')}/?default_format=JSONCompact",
        body=sql.encode(),
        headers=basic(user, ctx.secret(password_secret)),
        timeout_s=300,
    )
    return out["data"] if out else []  # type: ignore[index]


def _one(ctx: StackContext, sql: str) -> object:
    rows = _query(ctx, sql)
    return rows[0][0] if rows else None


def _between(col: str, w: WallWindow) -> str:
    return f"{col} >= fromUnixTimestamp64Nano({w.start}) AND {col} < fromUnixTimestamp64Nano({w.end})"


def _attr(key: str, value: str) -> str:
    if key == "service.name":
        return f"ServiceName = {_lit(value)}"
    k = _lit(key)
    return f"if(mapContains(Attributes, {k}), Attributes[{k}], ResourceAttributes[{k}]) = {_lit(value)}"


def _series(metric: str, attrs: tuple[tuple[str, str], ...], w: WallWindow) -> str:
    return " AND ".join([f"MetricName = {_lit(metric)}", _between("TimeUnix", w)] + [_attr(k, v) for k, v in attrs])


def probe(check: Check, window: WallWindow, ctx: StackContext) -> Probe:
    w = window
    match check:
        case CountLogs(service=svc, error_only=err, contains=sub):
            cond = [_between("Timestamp", w)]
            if svc is not None:
                cond.append(f"ServiceName = {_lit(svc)}")
            if err:
                cond.append(ERROR_LOG)
            if sub is not None:
                cond.append(f"position(Body, {_lit(sub)}) > 0")
            return Exact(int(_one(ctx, f"SELECT count() FROM otel.otel_logs WHERE {' AND '.join(cond)}")))  # type: ignore[arg-type]
        case CountSpans(service=svc, errors_only=err, kind=kind):
            cond = [_between("Timestamp", w)]
            if svc is not None:
                cond.append(f"ServiceName = {_lit(svc)}")
            if err:
                cond.append("StatusCode = 'Error'")
            if kind is not None:
                cond.append(f"SpanKind = {_lit(KIND[kind])}")
            return Exact(int(_one(ctx, f"SELECT count() FROM otel.otel_traces WHERE {' AND '.join(cond)}")))  # type: ignore[arg-type]
        case TraceSpanCount(trace_id=tid):
            sql = f"SELECT count() FROM otel.otel_traces WHERE TraceId = {_lit(tid)} AND {_between('Timestamp', w)}"
            return Exact(int(_one(ctx, sql)))  # type: ignore[arg-type]
        case DistinctTraces(service=svc):
            cond = [_between("Timestamp", w)] + ([f"ServiceName = {_lit(svc)}"] if svc is not None else [])
            return Exact(int(_one(ctx, f"SELECT uniqExact(TraceId) FROM otel.otel_traces WHERE {' AND '.join(cond)}")))  # type: ignore[arg-type]
        case SeriesPoints(metric=m, attrs=attrs):
            parts = [
                f"SELECT count() AS n FROM otel.otel_metrics_{t} WHERE {_series(m, attrs, w)}" for t in METRIC_TABLES
            ]
            return Exact(int(_one(ctx, f"SELECT sum(n) FROM ({' UNION ALL '.join(parts)})")))  # type: ignore[arg-type]
        case SeriesLast(metric=m, attrs=attrs):
            parts = [
                f"SELECT TimeUnix, Value FROM otel.otel_metrics_{t} WHERE {_series(m, attrs, w)}"
                for t in ("sum", "gauge")
            ]
            rows = _query(
                ctx, f"SELECT Value FROM ({' UNION ALL '.join(parts)}) ORDER BY TimeUnix DESC, Value DESC LIMIT 1"
            )
            if not rows:
                return Exact(None)
            v = float(rows[0][0])
            return Exact(int(v) if v.is_integer() else v)
        case MetricNames():
            parts = [
                f"SELECT DISTINCT MetricName FROM otel.otel_metrics_{t} WHERE {_between('TimeUnix', w)}"
                for t in METRIC_TABLES
            ]
            return Exact(sorted({metric_key(r[0]) for r in _query(ctx, " UNION ALL ".join(parts))}))
        case MaxAttrLen(signal=sig, key=key):
            table, col = ("otel_traces", "SpanAttributes") if sig == "traces" else ("otel_logs", "LogAttributes")
            rows = _query(
                ctx,
                f"SELECT count(), max(lengthUTF8({col}[{_lit(key)}])) FROM otel.{table} "
                f"WHERE mapContains({col}, {_lit(key)}) AND {_between('Timestamp', w)}",
            )
            return Exact(int(rows[0][1]) if int(rows[0][0]) else None)
        case IngestCounters():
            return Exact(dict(collector_counters(ctx)))


def readonly_agent_user(ctx: StackContext) -> bool:
    """otb_agent: SELECT on otel.* under the otb_agent settings profile. True when it changed."""
    try:
        _query(ctx, "SELECT 1", ctx.secret("B3_CH_AGENT_USER"), "B3_CH_AGENT_PASSWORD")
        return False
    except Exception:
        pass
    user, pw = ctx.secret("B3_CH_AGENT_USER"), ctx.secret("B3_CH_AGENT_PASSWORD")
    _query(
        ctx, f"CREATE USER OR REPLACE {user} IDENTIFIED WITH sha256_password BY {_lit(pw)} SETTINGS PROFILE 'otb_agent'"
    )
    _query(ctx, f"GRANT SELECT ON otel.* TO {user}")
    return True


def provisioning() -> Mapping[str, Callable[[StackContext], bool]]:
    return {"readonly_agent_user": readonly_agent_user}


def is_empty(ctx: StackContext) -> bool:
    rows = _one(ctx, "SELECT sum(total_rows) FROM system.tables WHERE database = 'otel'")
    return int(rows or 0) == 0  # type: ignore[arg-type]
