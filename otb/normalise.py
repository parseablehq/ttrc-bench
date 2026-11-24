"""The normaliser: one OTLP/JSON export request in, the bytes every stack receives out.

It is a versioned pure function shared by the live gateway and `corpus import-v2`, applied before the archive,
so all four stacks are fed identical bytes and no per-stack collector carries a transform. Its rules are harness
v2's two whole-batch-rejection workarounds plus the session-replay drop (DESIGN amendment 11), and since n2 the
scrub of the flag injector's fingerprints (SPEC §5, interrogate-1 item 1): flagd's own telemetry and every
OpenFeature attribute, event and metric the demo's services emit would tell an agent which flag is on.
Changing a rule changes corpus bytes, which makes a new corpus: bump VERSION.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import orjson

Signal = Literal["traces", "logs", "metrics"]

VERSION = "n2"
RULES: dict[str, str] = {
    "rr_web_records": "logs: drop log records of instrumentation scope rum.rr-web",
    "empty_attr_deleted": "logs: delete a log record attribute whose AnyValue is empty",
    "empty_body_set": 'logs: a missing or empty log body becomes stringValue ""',
    "flag_service_records": "all: drop every resource whose service.name is flagd or flagd-ui, with its records",
    "flag_events": "traces: drop span events whose name contains feature_flag",
    "flag_metric_points": "metrics: drop metrics whose name contains feature_flag, with their points",
    "flag_attrs": "all: delete attributes whose key contains feature_flag, on resources, scopes, spans, span "
    "events, span links, log records, data points and exemplars",
}
METRIC_KINDS = ("sum", "gauge", "histogram", "exponentialHistogram", "summary")
RR_WEB = "rum.rr-web"
FLAG_SERVICES = frozenset({"flagd", "flagd-ui"})
FLAG_MARK = "feature_flag"
RESOURCE_LISTS: dict[Signal, str] = {"traces": "resourceSpans", "logs": "resourceLogs", "metrics": "resourceMetrics"}


@dataclass(frozen=True, slots=True)
class Normalised:
    body: bytes
    records: int
    min_event_ns: int
    max_event_ns: int
    dropped: dict[str, int] = field(default_factory=dict)


def source_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def normalise(signal: Signal, raw: bytes) -> Normalised:
    """Records are spans, log records or metric data points after the rules. Event times are the non-zero
    span start/end/event times, log time/observed time, and data point times. A metric point's startTimeUnixNano
    is the start of its cumulative series, not an event, so it is excluded."""
    doc = orjson.loads(raw)
    times: list[int] = []
    dropped = {k: 0 for k in RULES}
    _drop_flag_services(signal, doc, dropped)
    if signal == "traces":
        records = _spans(doc, times, dropped)
    elif signal == "logs":
        records = _logs(doc, times, dropped)
    else:
        records = _points(doc, times, dropped)
    nonzero = [t for t in times if t]
    return Normalised(
        body=orjson.dumps(doc),
        records=records,
        min_event_ns=min(nonzero, default=0),
        max_event_ns=max(nonzero, default=0),
        dropped={k: v for k, v in dropped.items() if v},
    )


def _ns(v: object) -> int:
    return int(v) if v else 0


def _service(resource: dict | None) -> str | None:
    for a in (resource or {}).get("attributes") or ():
        if a.get("key") == "service.name":
            return (a.get("value") or {}).get("stringValue")
    return None


def _metric_points(m: dict) -> int:
    return sum(len((m.get(kind) or {}).get("dataPoints") or ()) for kind in METRIC_KINDS)


def _count_records(signal: Signal, r: dict) -> int:
    if signal == "traces":
        return sum(len(ss.get("spans") or ()) for ss in r.get("scopeSpans") or ())
    if signal == "logs":
        return sum(len(sl.get("logRecords") or ()) for sl in r.get("scopeLogs") or ())
    return sum(_metric_points(m) for sm in r.get("scopeMetrics") or () for m in sm.get("metrics") or ())


def _drop_flag_services(signal: Signal, doc: dict, dropped: dict[str, int]) -> None:
    key = RESOURCE_LISTS[signal]
    if key not in doc:
        return
    kept = []
    for r in doc[key] or ():
        if _service(r.get("resource")) in FLAG_SERVICES:
            dropped["flag_service_records"] += _count_records(signal, r)
        else:
            _scrub(r.get("resource"), dropped)
            kept.append(r)
    doc[key] = kept


def _scrub(obj: dict | None, dropped: dict[str, int], key: str = "attributes") -> None:
    attrs = obj.get(key) if obj else None
    if attrs:
        kept = [a for a in attrs if FLAG_MARK not in a.get("key", "")]
        if len(kept) != len(attrs):
            dropped["flag_attrs"] += len(attrs) - len(kept)
            obj[key] = kept  # type: ignore[index]


def _spans(doc: dict, times: list[int], dropped: dict[str, int]) -> int:
    n = 0
    for rs in doc.get("resourceSpans") or ():
        for ss in rs.get("scopeSpans") or ():
            _scrub(ss.get("scope"), dropped)
            for sp in ss.get("spans") or ():
                n += 1
                _scrub(sp, dropped)
                events = sp.get("events")
                if events:
                    kept = [e for e in events if FLAG_MARK not in (e.get("name") or "")]
                    if len(kept) != len(events):
                        dropped["flag_events"] += len(events) - len(kept)
                        sp["events"] = kept
                    for e in kept:
                        _scrub(e, dropped)
                for link in sp.get("links") or ():
                    _scrub(link, dropped)
                times.append(_ns(sp.get("startTimeUnixNano")))
                times.append(_ns(sp.get("endTimeUnixNano")))
                times.extend(_ns(e.get("timeUnixNano")) for e in sp.get("events") or ())
    return n


def _logs(doc: dict, times: list[int], dropped: dict[str, int]) -> int:
    n = 0
    for rl in doc.get("resourceLogs") or ():
        for sl in rl.get("scopeLogs") or ():
            _scrub(sl.get("scope"), dropped)
            records = sl.get("logRecords") or []
            if (sl.get("scope") or {}).get("name") == RR_WEB:
                dropped["rr_web_records"] += len(records)
                sl["logRecords"] = []
                continue
            for r in records:
                n += 1
                times.append(_ns(r.get("timeUnixNano")))
                times.append(_ns(r.get("observedTimeUnixNano")))
                attrs = r.get("attributes")
                if attrs:
                    kept = [a for a in attrs if a.get("value")]
                    if len(kept) != len(attrs):
                        dropped["empty_attr_deleted"] += len(attrs) - len(kept)
                        r["attributes"] = kept
                _scrub(r, dropped)
                if not r.get("body"):
                    dropped["empty_body_set"] += 1
                    r["body"] = {"stringValue": ""}
    return n


def _points(doc: dict, times: list[int], dropped: dict[str, int]) -> int:
    n = 0
    for rm in doc.get("resourceMetrics") or ():
        for sm in rm.get("scopeMetrics") or ():
            _scrub(sm.get("scope"), dropped)
            metrics = sm.get("metrics")
            if metrics:
                flagged = [m for m in metrics if FLAG_MARK in (m.get("name") or "")]
                if flagged:
                    dropped["flag_metric_points"] += sum(_metric_points(m) for m in flagged)
                    sm["metrics"] = [m for m in metrics if FLAG_MARK not in (m.get("name") or "")]
            for m in sm.get("metrics") or ():
                for kind in METRIC_KINDS:
                    data = m.get(kind)
                    if data:
                        for p in data.get("dataPoints") or ():
                            n += 1
                            _scrub(p, dropped)
                            for ex in p.get("exemplars") or ():
                                _scrub(ex, dropped, "filteredAttributes")
                            times.append(_ns(p.get("timeUnixNano")))
    return n
