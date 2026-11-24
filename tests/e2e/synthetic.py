"""Hand-made OTLP/JSON corpora for measure tests: each frame its own archive segment, so its rows land in their own
Parquet file and a reader can be made to see them in any order."""

from __future__ import annotations

from pathlib import Path

import orjson

from otb.archive import ArchiveWriter, Header, NormaliserStamp, SourcePins
from otb.normalise import normalise
from otb.ref.build import build

NS = 1_000_000_000
MS = 1_000_000
SOURCE = SourcePins("synthetic", "none", "none", {}, "none")


def kv(key: str, value: object) -> dict:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"stringValue": str(value)}}


def span(
    trace: str,
    sid: str,
    parent: str | None,
    name: str,
    kind: int,
    start_ns: int,
    dur_ms: int,
    *,
    error: str | None = None,
    attrs: dict | None = None,
) -> dict:
    out = {
        "traceId": trace,
        "spanId": sid,
        "name": name,
        "kind": kind,
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(start_ns + dur_ms * MS),
        "attributes": [kv(k, v) for k, v in (attrs or {}).items()],
    }
    if parent:
        out["parentSpanId"] = parent
    if error is not None:
        out["status"] = {"code": 2, "message": error}
    return out


def resource(service: str, version: str = "1.0") -> dict:
    return {"attributes": [kv("service.name", service), kv("service.version", version)]}


def traces(service: str, *spans: dict) -> tuple[str, dict]:
    return "traces", {"resourceSpans": [{"resource": resource(service), "scopeSpans": [{"spans": list(spans)}]}]}


def logs(service: str, *records: dict) -> tuple[str, dict]:
    return "logs", {"resourceLogs": [{"resource": resource(service), "scopeLogs": [{"logRecords": list(records)}]}]}


def log(t_ns: int, body: str, severity: int = 9, trace: str = "", sid: str = "") -> dict:
    return {
        "timeUnixNano": str(t_ns),
        "severityNumber": severity,
        "body": {"stringValue": body},
        "traceId": trace,
        "spanId": sid,
    }


def metric(service: str, name: str, kind: str, points: list[dict], monotonic: bool = True) -> tuple[str, dict]:
    data: dict = {"dataPoints": points}
    if kind == "sum":
        data |= {"aggregationTemporality": 2, "isMonotonic": monotonic}
    return "metrics", {
        "resourceMetrics": [
            {"resource": resource(service), "scopeMetrics": [{"metrics": [{"name": name, kind: data}]}]}
        ]
    }


def point(t_ns: int, value: int | float, start_ns: int = 0, **attrs: str) -> dict:
    out = {"timeUnixNano": str(t_ns), "attributes": [kv(k, v) for k, v in attrs.items()]}
    if start_ns:
        out["startTimeUnixNano"] = str(start_ns)
    out |= {"asInt": str(value)} if isinstance(value, int) else {"asDouble": value}
    return out


def write_corpus(root: Path, frames: list[tuple[str, dict]], workers: int = 2) -> Path:
    """One segment per frame (rotation after every frame), sealed and built."""
    header = Header(root.name, ("default",), SOURCE, NormaliserStamp.current(), rotate_bytes=1)
    w = ArchiveWriter(root, header, rotate_s=None, dedupe=False)
    for signal, doc in frames:
        w.append(signal, normalise(signal, orjson.dumps(doc)), received_ns=0)
    w.seal(scenario_sha256=None, schedule_sha256=None)
    w.close()
    build(root, workers=workers)
    return root
