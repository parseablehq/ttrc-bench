"""B1 Prometheus/Loki/Tempo: gate probes in PromQL, LogQL and the Tempo HTTP API, provisioning, emptiness.

Windows are half-open [start, end). Prometheus and Loki range selectors are left-open, right-closed, so each
instant query is evaluated one tick before the window end over exactly the window's length: Prometheus samples
are millisecond precision, Loki entries nanosecond precision.

Tempo span checks enumerate with TraceQL search and count the matched spans each result carries (spss large
enough to return every match, checked against the spanset's `matched` count), keeping the spans that start in the
window. Search is complete once the live-store dates its blocks by the spans' own times: tempo.yaml raises
`live_store.wal.ingestion_time_range_slack`, whose 2 m default made U4's searches return 0, 444 or 1940 traces
for the same query, and keeps a trace in one row (max_trace_idle, max_trace_live), because the search combiner
keeps only the larger spanset of a trace split across rows (docs/v1/units/U5-U6.md). A result at the trace
limit, or a spanset with fewer spans than it matched, is Unverifiable rather than a short count.

A trace whose spans arrive over more than max_trace_live (SEARCH_LIVE_WINDOW_S) still splits, and the response
cannot show it. The gate picks those traces from the archive, fetches each by id once (ctx.by_id), and the span
checks count their fetched spans instead of the search's, noting the spans search missed.
"""

from __future__ import annotations

import base64
import re
import time
import urllib.parse
from collections import Counter
from collections.abc import Callable, Mapping
from pathlib import Path

import yaml

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
    StackContext,
    TraceSpanCount,
    Unverifiable,
    basic,
    collector_counters,
)
from otb.timebase import NS, WallWindow

SA_NAME = "otb-agent"
SEARCH_LIMIT = 100_000  # traces per search; a result this large is reported as truncated
SEARCH_SPSS = 100_000  # spans per spanset: every matching span (tempo.yaml lifts the frontend's cap)
SEARCH_MIN_SPLIT_S = 60
LOKI_PAGE = 5000  # the configured max_entries_limit_per_query
HISTOGRAM_PARTS = ("_bucket", "_sum", "_count")
KIND = {"SERVER": "server", "CLIENT": "client"}
ERROR_FILTER = '| severity_number >= 17 or severity_text =~ "(?i)(error|fatal)"'
GO_DURATION = re.compile(r"(\d+(?:\.\d+)?)(h|m|s)")


def _seconds(go_duration: str) -> float:
    parts = GO_DURATION.findall(go_duration)
    if "".join(n + u for n, u in parts) != go_duration:
        raise ValueError(f"unsupported duration {go_duration!r}")
    return sum(float(n) * {"h": 3600, "m": 60, "s": 1}[u] for n, u in parts)


SEARCH_LIVE_WINDOW_S = _seconds(
    yaml.safe_load((Path(__file__).parent / "config" / "tempo.yaml").read_text())["live_store"]["max_trace_live"]
)


def _label(key: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", key)


def _q(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _get(ctx: StackContext, base: str, path: str, **params: object) -> dict:
    url = f"{ctx.url(base)}{path}?{urllib.parse.urlencode({k: str(v) for k, v in params.items()})}"
    return ctx.http_json("GET", url, timeout_s=300)  # type: ignore[return-value]


# ---------- Prometheus ----------


def _prom_range(ctx: StackContext, selector: str, w: WallWindow) -> list[dict]:
    """Raw samples of `selector` with timestamps in [start, end)."""
    ms = (w.end - w.start) // 1_000_000
    t = (w.end // 1_000_000 - 1) / 1000
    return _get(ctx, "prometheus", "/api/v1/query", query=f"{selector}[{ms}ms]", time=f"{t:.3f}")["data"]["result"]


def _prom_names(ctx: StackContext, w: WallWindow) -> set[str]:
    """Names with a sample in [start, end). Every raw sample of every series overruns query.max-samples (gke-24h:
    145k background series), so the label index names the candidates, a superset at block granularity, and one
    count_over_time per name, which Prometheus evaluates a series at a time, keeps those with a sample."""
    ms = (w.end - w.start) // 1_000_000
    t = (w.end // 1_000_000 - 1) / 1000
    span = {"start": f"{w.start / NS:.3f}", "end": f"{t:.3f}"}
    candidates = _get(ctx, "prometheus", "/api/v1/label/__name__/values", **span)["data"]
    return {
        n
        for n in candidates
        if _get(
            ctx,
            "prometheus",
            "/api/v1/query",
            query=f"count(count_over_time({{__name__={_q(n)}}}[{ms}ms]))",
            time=f"{t:.3f}",
        )["data"]["result"]
    }


def _families(names: set[str]) -> dict[str, str]:
    """Prometheus series name -> metric family (histogram parts fold into their base name)."""
    bases = {n.removesuffix("_bucket") for n in names if n.endswith("_bucket")}
    out = {}
    for n in names:
        base = next((n.removesuffix(p) for p in HISTOGRAM_PARTS if n.endswith(p) and n.removesuffix(p) in bases), n)
        out[n] = base
    return out


# Prometheus 3.15's default OTLP translation (UnderscoreEscapingWithSuffixes), as implemented by
# github.com/prometheus/otlptranslator v1.0.0 normalizeName: name tokens, unit suffixes, then _total / _ratio.
UNITS = {
    "d": "days", "h": "hours", "min": "minutes", "s": "seconds", "ms": "milliseconds", "us": "microseconds",
    "ns": "nanoseconds", "By": "bytes", "KiBy": "kibibytes", "MiBy": "mebibytes", "GiBy": "gibibytes",
    "TiBy": "tibibytes", "KBy": "kilobytes", "MBy": "megabytes", "GBy": "gigabytes", "TBy": "terabytes",
    "m": "meters", "V": "volts", "A": "amperes", "J": "joules", "W": "watts", "g": "grams", "Cel": "celsius",
    "Hz": "hertz", "1": "", "%": "percent",
}  # fmt: skip
PER_UNITS = {"s": "second", "m": "minute", "h": "hour", "d": "day", "w": "week", "mo": "month", "y": "year"}
_INVALID = re.compile(r"[^a-zA-Z0-9:]")


def _clean_unit(unit: str) -> str:
    return re.sub(r"__+", "_", _INVALID.sub("_", unit)).removeprefix("_")


def metric_name(name: str, unit: str, type: str, monotonic: bool) -> str:
    """The Prometheus metric family an OTel metric is stored as."""
    tokens = [t for t in _INVALID.split(name) if t]
    main_otel, _, per_otel = (unit or "").partition("/")
    main_otel, per_otel = main_otel.strip(), per_otel.strip()
    main = UNITS.get(main_otel, main_otel) if main_otel and not set("{}") & set(main_otel) else ""
    per = PER_UNITS.get(per_otel, per_otel) if per_otel and not set("{}") & set(per_otel) else ""
    main, per = _clean_unit(main), _clean_unit("per_" + per if per else "")
    if main in tokens:
        main = ""
    per = "" if per == "per_" else per.removesuffix("_")
    if per in tokens:
        per = ""
    if per:
        main = main.removesuffix("_")
    tokens += [t for t in (main, per) if t]
    if type == "sum" and monotonic:
        tokens = [t for t in tokens if t != "total"] + ["total"]
    if unit == "1" and type == "gauge":
        tokens = [t for t in tokens if t != "ratio"] + ["ratio"]
    out = "_".join(tokens)
    return "_" + out if out[:1].isdigit() else out


def _prom_selector(check: SeriesPoints | SeriesLast) -> str:
    family = metric_name(check.metric, check.unit, check.type, check.monotonic)
    name = f"{family}_count" if check.type == "histogram" else family
    matchers = ",".join(f"{_label(k)}={_q(v)}" for k, v in check.attrs)
    return f"{name}{{{matchers}}}"


# ---------- Loki ----------


def _loki_instant(ctx: StackContext, query: str, w: WallWindow) -> int:
    res = _get(ctx, "loki", "/loki/api/v1/query", query=query, time=w.end - 1)["data"]["result"]
    return sum(int(float(r["value"][1])) for r in res)


def _loki_lines(ctx: StackContext, query: str, w: WallWindow) -> list[tuple[dict, int, str]]:
    """Every (labels, ts, line) of a log query in [start, end), paged forward by timestamp."""
    out: list[tuple[dict, int, str]] = []
    start, seen = w.start, Counter[tuple[int, str]]()
    while True:
        res = _get(
            ctx,
            "loki",
            "/loki/api/v1/query_range",
            query=query,
            start=start,
            end=w.end,
            limit=LOKI_PAGE,
            direction="forward",
        )["data"]["result"]
        page = sorted(((r["stream"], int(ts), line) for r in res for ts, line in r["values"]), key=lambda x: x[1])
        fresh = [p for p in page if seen[(p[1], p[2])] == 0]
        for p in fresh:
            seen[(p[1], p[2])] += 1
        out += fresh
        if len(page) < LOKI_PAGE:
            return out
        start = page[-1][1]  # entries at this nanosecond are re-read and dropped by `seen`


def _service_selector(service: str | None) -> str:
    return f"{{service_name={_q(service)}}}" if service is not None else '{service_name=~".+"}'


# ---------- Tempo ----------


def fetch_trace(ctx: StackContext, trace_id: str) -> list[dict] | None:
    """All spans of one trace with their service name, or None when Tempo does not have it."""
    r = ctx.http("GET", f"{ctx.url('tempo')}/api/v2/traces/{trace_id}", timeout_s=120)
    if r.status == 404:
        return None
    if r.status != 200:
        raise RuntimeError(f"tempo trace {trace_id}: HTTP {r.status}: {r.body[:200]!r}")
    body = r.json()
    spans = []
    for rs in (body.get("trace") or {}).get("resourceSpans", []):  # type: ignore[union-attr]
        res = {a["key"]: next(iter(a["value"].values()), None) for a in rs.get("resource", {}).get("attributes", [])}
        for ss in rs.get("scopeSpans", []):
            for sp in ss.get("spans", []):
                spans.append({**sp, "service": res.get("service.name")})
    return spans


class Truncated(Exception):
    pass


def _search_spans(ctx: StackContext, traceql: str, w: WallWindow) -> dict[str, list[dict]]:
    """trace id -> the spans of that trace that match the query and start in the window. A response over the
    querier's size limit splits the window in halves, whose span sets join by span id per trace."""
    try:
        return _search_once(ctx, traceql, w)
    except HttpError as e:
        if b"response larger than the max" not in e.response.body:
            raise
        if w.end - w.start <= SEARCH_MIN_SPLIT_S * NS:
            raise Truncated(f"a {SEARCH_MIN_SPLIT_S} s search is over the response size limit") from e
    mid = w.start + (w.end - w.start) // 2
    out = _search_spans(ctx, traceql, WallWindow(w.start, mid))
    for tid, spans in _search_spans(ctx, traceql, WallWindow(mid, w.end)).items():
        out[tid] = list({s["spanID"]: s for s in out.get(tid, []) + spans}.values())
    return out


def _search_once(ctx: StackContext, traceql: str, w: WallWindow) -> dict[str, list[dict]]:
    res = _get(
        ctx,
        "tempo",
        "/api/search",
        q=traceql,
        start=w.start // NS - 300,
        end=w.end // NS + 300,
        limit=SEARCH_LIMIT,
        spss=SEARCH_SPSS,
    )
    traces = res.get("traces", [])
    if len(traces) >= SEARCH_LIMIT:
        raise Truncated(f"search returned the limit of {SEARCH_LIMIT} traces")
    out: dict[str, list[dict]] = {}
    for t in traces:
        sets = t.get("spanSets") or ([t["spanSet"]] if t.get("spanSet") else [])
        spans: dict[str, dict] = {}
        for ss in sets:
            if int(ss.get("matched", 0)) > len(ss.get("spans", [])):
                raise Truncated(f"a spanset matched {ss['matched']} spans and returned {len(ss['spans'])}")
            spans |= {sp["spanID"]: sp for sp in ss.get("spans", [])}
        out[t["traceID"].rjust(32, "0")] = _spans_in(list(spans.values()), w)
    return out


def _spans_in(spans: list[dict], w: WallWindow) -> list[dict]:
    return [s for s in spans if w.start <= int(s["startTimeUnixNano"]) < w.end]


def _span_filter(service: str | None, errors_only: bool, kind: str | None) -> str:
    conds = []
    if service is not None:
        conds.append(f"resource.service.name = {_q(service)}")
    if errors_only:
        conds.append("status = error")
    if kind is not None:
        conds.append(f"kind = {KIND[kind]}")
    return "{" + " && ".join(conds) + "}"


def _span_matches(service: str | None, errors_only: bool, kind: str | None) -> Callable[[dict], bool]:
    """The search filter of _span_filter, applied to a span fetched by id (OTLP JSON)."""
    return lambda s: (
        (service is None or s["service"] == service)
        and (not errors_only or (s.get("status") or {}).get("code") == "STATUS_CODE_ERROR")
        and (kind is None or s.get("kind") == f"SPAN_KIND_{kind}")
    )


def _has_attr(key: str) -> Callable[[dict], bool]:
    return lambda s: any(a["key"] == key for a in s.get("attributes", []))


def _with_by_id(
    found: dict[str, list[dict]], ctx: StackContext, w: WallWindow, keep: Callable[[dict], bool]
) -> tuple[dict[str, list[dict]], dict | None]:
    """Search spans, with each trace the gate fetched by id replaced by its fetched spans that match and start in
    the window. Search can return one fragment of a trace whose spans arrived over more than max_trace_live;
    the fetch by id is exact. The note names the spans search missed and their traces."""
    out = dict(found)
    missed, traces = [], []
    for tid, spans in ctx.by_id.items():
        stored = [s | {"spanID": _hex_id(s["spanId"])} for s in _spans_in(spans, w) if keep(s)]
        if not stored and tid not in found:
            continue
        seen = {s["spanID"] for s in found.get(tid, [])}
        extra = sorted(s["spanID"] for s in stored if s["spanID"] not in seen)
        if extra:
            missed += extra
            traces.append(tid)
        out[tid] = stored
    return out, ({"spans": sorted(missed), "traces": sorted(traces)} if missed else None)


def _hex_id(b64: str) -> str:
    return base64.b64decode(b64).hex()


def _tempo_probe(check: Check, w: WallWindow, ctx: StackContext) -> Probe:
    try:
        match check:
            case CountSpans(service=svc, errors_only=err, kind=kind):
                found = _search_spans(ctx, _span_filter(svc, err, kind), w)
                found, note = _with_by_id(found, ctx, w, _span_matches(svc, err, kind))
                return Exact(sum(len(v) for v in found.values()), note)
            case DistinctTraces(service=svc):
                found = _search_spans(ctx, _span_filter(svc, False, None), w)
                found, note = _with_by_id(found, ctx, w, _span_matches(svc, False, None))
                return Exact(sum(1 for v in found.values() if v), note)
            case MaxAttrLen(key=key):
                found = _search_spans(ctx, f"{{span.{_q(key)} != nil}} | select(span.{_q(key)})", w)
                found, note = _with_by_id(found, ctx, w, _has_attr(key))
                lengths = [
                    len(a["value"]["stringValue"])
                    for spans in found.values()
                    for sp in spans
                    for a in sp.get("attributes", [])
                    if a["key"] == key and "stringValue" in a["value"]
                ]
                return Exact(max(lengths) if lengths else None, note)
    except Truncated as e:
        return Unverifiable(str(e))
    raise AssertionError(check)


# ---------- the probe ----------


def probe(check: Check, window: WallWindow, ctx: StackContext) -> Probe:
    w = window
    match check:
        case CountLogs(service=svc, error_only=err, contains=sub):
            pipeline = (f" |= {_q(sub)}" if sub is not None else "") + (f" {ERROR_FILTER}" if err else "")
            ms = (w.end - w.start) // 1_000_000
            return Exact(_loki_instant(ctx, f"sum(count_over_time({_service_selector(svc)}{pipeline} [{ms}ms]))", w))
        case CountSpans() | DistinctTraces() | MaxAttrLen(signal="traces"):
            return _tempo_probe(check, w, ctx)
        case MaxAttrLen(signal="logs", key=key):
            label = _label(key)
            lines = _loki_lines(ctx, f'{{service_name=~".+"}} | {label} != ""', w)
            lengths = [len(labels[label]) for labels, _, _ in lines if label in labels]
            return Exact(max(lengths) if lengths else None)
        case TraceSpanCount(trace_id=tid):
            spans = fetch_trace(ctx, tid)
            return Exact(len(_spans_in(spans, w)) if spans else 0)
        case SeriesPoints():
            return Exact(sum(len(r["values"]) for r in _prom_range(ctx, _prom_selector(check), w)))
        case SeriesLast():
            samples = [(float(t), float(v)) for r in _prom_range(ctx, _prom_selector(check), w) for t, v in r["values"]]
            if not samples:
                return Exact(None)
            v = max(samples)[1]
            return Exact(int(v) if v.is_integer() else v)
        case MetricNames():
            fams = _families(_prom_names(ctx, w))
            return Exact(sorted({f for f in fams.values() if f != "target_info"}))
        case IngestCounters():
            return Exact(dict(collector_counters(ctx)))
    raise AssertionError(check)


# ---------- provisioning ----------


def _admin(ctx: StackContext) -> dict[str, str]:
    return basic(ctx.secret("B1_GRAFANA_ADMIN_USER"), ctx.secret("B1_GRAFANA_ADMIN_PASSWORD"))


def viewer_service_account(ctx: StackContext) -> bool:
    """A Viewer service account and token for mcp-grafana. True when it changed."""
    base = ctx.url("grafana")
    if ctx.has_secret("B1_GRAFANA_MCP_TOKEN"):
        auth = {"Authorization": f"Bearer {ctx.secret('B1_GRAFANA_MCP_TOKEN')}"}
        if ctx.http("GET", f"{base}/api/user", headers=auth).status == 200:
            return False
    found = ctx.http_json("GET", f"{base}/api/serviceaccounts/search?query={SA_NAME}", headers=_admin(ctx))
    accounts = [a for a in found["serviceAccounts"] if a["name"] == SA_NAME]  # type: ignore[index]
    if accounts:
        sa_id = accounts[0]["id"]
    else:
        created = ctx.http_json(
            "POST", f"{base}/api/serviceaccounts", body={"name": SA_NAME, "role": "Viewer"}, headers=_admin(ctx)
        )
        sa_id = created["id"]  # type: ignore[index]
    token = ctx.http_json(
        "POST",
        f"{base}/api/serviceaccounts/{sa_id}/tokens",
        body={"name": f"{SA_NAME}-{int(time.time())}"},
        headers=_admin(ctx),
    )
    ctx.set_secret("B1_GRAFANA_MCP_TOKEN", token["key"])  # type: ignore[index]
    return True


def provisioning() -> Mapping[str, Callable[[StackContext], bool]]:
    return {"viewer_service_account": viewer_service_account}


def is_empty(ctx: StackContext) -> bool:
    names = _get(ctx, "prometheus", "/api/v1/label/__name__/values")["data"]
    streams = _get(ctx, "loki", "/loki/api/v1/labels").get("data")
    traces = _get(ctx, "tempo", "/api/search", q="{}", limit=1).get("traces")
    return not names and not streams and not traces
