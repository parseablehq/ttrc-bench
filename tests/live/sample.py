"""The small live sample: the first N lines of each dry-run recording file (OTLP/JSON export requests),
normalised by DESIGN amendment 11's three rules (what the archive normaliser will apply before every stack),
with every non-zero *UnixNano shifted by one whole number of minutes so the newest lands about a minute ago.

Expected values are computed here from the shifted bytes alone, independently of every stack.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

RECORDING = Path(__file__).resolve().parents[2] / "corpus/dryrun/recording"
NS = 1_000_000_000
TOP = {
    "traces": ("resourceSpans", "scopeSpans", "spans"),
    "logs": ("resourceLogs", "scopeLogs", "logRecords"),
    "metrics": ("resourceMetrics", "scopeMetrics", "metrics"),
}
_TS = re.compile(rb'(UnixNano":")(\d+)(")')


def _empty(v: object) -> bool:
    return v is None or v == {}


def normalise(signal: str, req: dict) -> dict:
    """Amendment 11: drop log attributes whose AnyValue is empty, replace a null/empty log body with "",
    drop log records of scope rum.rr-web."""
    if signal != "logs":
        return req
    for rl in req["resourceLogs"]:
        rl["scopeLogs"] = [sl for sl in rl["scopeLogs"] if (sl.get("scope") or {}).get("name") != "rum.rr-web"]
        for sl in rl["scopeLogs"]:
            for r in sl.get("logRecords", []):
                r["attributes"] = [a for a in r.get("attributes", []) if not _empty(a.get("value"))]
                if _empty(r.get("body")):
                    r["body"] = {"stringValue": ""}
    return req


@dataclass(frozen=True)
class Sample:
    shift_s: int
    bodies: dict[str, list[bytes]]  # per signal, in file order


def build(lines: int = 60, now_ns: int | None = None) -> Sample:
    raw = {
        s: [normalise(s, json.loads(line)) for line in (RECORDING / f"{s}.jsonl").open().readlines()[:lines]]
        for s in TOP
    }
    enc = {s: [json.dumps(r, separators=(",", ":")).encode() for r in reqs] for s, reqs in raw.items()}
    newest = max(int(m.group(2)) for bodies in enc.values() for b in bodies for m in _TS.finditer(b))
    now_ns = now_ns or time.time_ns()
    shift_s = (now_ns - 60 * NS - newest) // NS // 60 * 60

    def shift(b: bytes) -> bytes:
        return _TS.sub(
            lambda m: (
                m.group(1) + (str(int(m.group(2)) + shift_s * NS).encode() if m.group(2) != b"0" else b"0") + m.group(3)
            ),
            b,
        )

    return Sample(shift_s, {s: [shift(b) for b in bodies] for s, bodies in enc.items()})


def post(base_url: str, signal: str, body: bytes, timeout_s: float = 120) -> tuple[int, str]:
    req = urllib.request.Request(
        f"{base_url}/v1/{signal}", data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            return r.status, r.read().decode(errors="replace")[:300]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:300]


def anyvalue(v: dict | None) -> object:
    if not v:
        return None
    ((k, x),) = v.items()
    if k in ("intValue",):
        return int(x)
    return x


def attrs(items: list | None) -> dict[str, object]:
    return {a["key"]: anyvalue(a.get("value")) for a in items or []}


@dataclass
class Records:
    spans: list[dict]
    logs: list[dict]
    points: list[dict]


def records(sample: Sample) -> Records:
    spans, logs, points = [], [], []
    for b in sample.bodies["traces"]:
        for rs in json.loads(b)["resourceSpans"]:
            res = attrs(rs.get("resource", {}).get("attributes"))
            for ss in rs["scopeSpans"]:
                for sp in ss.get("spans", []):
                    st = sp.get("status") or {}
                    spans.append(
                        {
                            "service": res.get("service.name"),
                            "trace_id": sp["traceId"].lower(),
                            "span_id": sp["spanId"],
                            "t": int(sp["startTimeUnixNano"]),
                            "kind": sp.get("kind"),
                            "error": st.get("code") in (2, "STATUS_CODE_ERROR"),
                            "attrs": attrs(sp.get("attributes")),
                        }
                    )
    for b in sample.bodies["logs"]:
        for rl in json.loads(b)["resourceLogs"]:
            res = attrs(rl.get("resource", {}).get("attributes"))
            for sl in rl["scopeLogs"]:
                for r in sl.get("logRecords", []):
                    t = int(r.get("timeUnixNano") or 0) or int(r["observedTimeUnixNano"])
                    body = r.get("body") or {}
                    logs.append(
                        {
                            "service": res.get("service.name"),
                            "t": t,
                            "sevnum": int(r.get("severityNumber") or 0),
                            "sevtext": (r.get("severityText") or "").lower(),
                            "body": body.get("stringValue", "") if isinstance(body, dict) else "",
                            "attrs": attrs(r.get("attributes")),
                        }
                    )
    for b in sample.bodies["metrics"]:
        for rm in json.loads(b)["resourceMetrics"]:
            res = attrs(rm.get("resource", {}).get("attributes"))
            for sm in rm["scopeMetrics"]:
                for m in sm.get("metrics", []):
                    for kind in ("gauge", "sum", "histogram", "exponentialHistogram", "summary"):
                        if kind not in m:
                            continue
                        for p in m[kind].get("dataPoints", []):
                            if kind in ("gauge", "sum"):
                                value = int(p["asInt"]) if "asInt" in p else float(p.get("asDouble", 0.0))
                            else:
                                value = int(p.get("count", 0))
                            points.append(
                                {
                                    "metric": m["name"],
                                    "kind": kind,
                                    "unit": m.get("unit", ""),
                                    "monotonic": bool(m[kind].get("isMonotonic")),
                                    "t": int(p["timeUnixNano"]),
                                    "value": value,
                                    "attrs": res | attrs(p.get("attributes")),
                                    "point_attrs": sorted(attrs(p.get("attributes"))),
                                    "exemplar": bool(p.get("exemplars")),
                                }
                            )
    return Records(spans, logs, points)


def stats(sample: Sample) -> dict[str, object]:
    r = records(sample)
    return {
        "shift_s": sample.shift_s,
        "requests": {s: len(b) for s, b in sample.bodies.items()},
        "bytes": {s: sum(map(len, b)) for s, b in sample.bodies.items()},
        "spans": len(r.spans),
        "logs": len(r.logs),
        "points": len(r.points),
        "point_kinds": dict(Counter(p["kind"] for p in r.points)),
    }
