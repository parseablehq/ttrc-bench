"""`otb ref build`: archive segments to Parquet, one file per (table, segment), from the archive only.

Idempotent and resumable: derived/ref/built.jsonl records, per segment, the segment sha, the builder sha and the
sha of every output file. A segment whose record matches and whose outputs are intact is skipped. Each worker
writes to a per-process temporary name and renames, so a killed build leaves no partial file under a real name,
and a rebuilt segment is byte-identical to the first build. Memory per worker is one row group per table.
"""

from __future__ import annotations

import hashlib
import os
import resource
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

import orjson
import pyarrow as pa
import pyarrow.parquet as pq

from otb.archive import Archive, SegmentEntry
from otb.host import describe as describe_host
from otb.host import peak_rss_bytes
from otb.ref.schema import (
    SCHEMA_VERSION,
    SPAN_KINDS,
    STATUS_CODES,
    TABLES,
    TABLES_BY_SIGNAL,
    TEMPORALITIES,
)

ROW_GROUP_ROWS = 50_000
MINUTE_NS = 60_000_000_000
METRIC_TYPES = {
    "sum": "sum",
    "gauge": "gauge",
    "histogram": "histogram",
    "exponentialHistogram": "exponential_histogram",
    "summary": "summary",
}


def builder_sha256() -> str:
    here = Path(__file__).parent
    return hashlib.sha256((here / "build.py").read_bytes() + (here / "schema.py").read_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class BuiltSegment:
    signal: str
    stream: str
    seq: int
    segment_sha256: str
    schema_version: str
    builder_sha256: str
    outputs: dict[str, dict]
    raw_bytes: int


@dataclass(frozen=True, slots=True)
class BuildReport:
    segments_total: int
    segments_built: int
    segments_skipped: int
    rows: dict[str, int]
    seconds: float
    bytes_in: int
    bytes_in_raw: int
    mb_per_s_raw: float
    peak_rss_parent_bytes: int
    peak_rss_worker_bytes: int
    workers: int
    host: dict


def anyvalue_str(v: dict | None) -> str | None:
    """The one stringification rule for attribute values."""
    if not v:
        return None
    for k, x in v.items():
        if k == "stringValue":
            return x
        if k == "intValue":
            return str(int(x))
        if k == "doubleValue":
            return repr(float(x))
        if k == "boolValue":
            return "true" if x else "false"
        if k == "bytesValue":
            return x
        return orjson.dumps(x, option=orjson.OPT_SORT_KEYS).decode()
    return None


def _attrs(items: list | None) -> list[tuple[str, str | None]]:
    return [(a["key"], anyvalue_str(a.get("value"))) for a in items or ()]


def _enum(v: object, names: dict[int, str], prefix: str) -> str:
    if isinstance(v, str):
        return v.removeprefix(prefix)
    return names.get(int(v or 0), str(v))


def _ns(v: object) -> int | None:
    return int(v) if v else None


SEVERITY_NAMES = {
    f"SEVERITY_NUMBER_{level}{'' if j == 1 else j}": 4 * i + j
    for i, level in enumerate(("TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"))
    for j in range(1, 5)
}


def _severity(v: object) -> int:
    if isinstance(v, str) and not v.isdigit():
        return SEVERITY_NAMES.get(v, 0)
    return int(v or 0)


def _hexid(v: str | None) -> str | None:
    return v or None


def _url_path(a: dict[str, str | None]) -> str | None:
    if a.get("url.path"):
        return a["url.path"]
    target = a.get("http.target")
    if target:
        return target.split("?", 1)[0]
    url = a.get("url.full") or a.get("http.url")
    if url:
        return urlsplit(url).path or "/"
    return None


def _http_status(a: dict[str, str | None]) -> int | None:
    v = a.get("http.response.status_code") or a.get("http.status_code")
    try:
        return int(v) if v else None
    except ValueError:
        return None


class _Sink:
    """Column buffers for one table, flushed to one Parquet row group every ROW_GROUP_ROWS rows."""

    def __init__(self, path: Path, schema: pa.Schema) -> None:
        self.path = path
        self.schema = schema
        self.tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        self.writer = pq.ParquetWriter(self.tmp, schema, compression="zstd")
        self.cols: list[list] = [[] for _ in schema]
        self.rows = 0

    def add(self, *values: object) -> None:
        for c, v in zip(self.cols, values, strict=True):
            c.append(v)
        if len(self.cols[0]) >= ROW_GROUP_ROWS:
            self.flush()

    def flush(self) -> None:
        if self.cols[0]:
            arrays = [pa.array(c, type=f.type) for c, f in zip(self.cols, self.schema, strict=True)]
            self.writer.write_table(pa.Table.from_arrays(arrays, schema=self.schema))
            self.rows += len(self.cols[0])
            self.cols = [[] for _ in self.schema]

    def close(self) -> dict:
        self.flush()
        self.writer.close()
        os.replace(self.tmp, self.path)
        sha = hashlib.sha256(self.path.read_bytes()).hexdigest()
        return {"path": self.path.name, "sha256": sha, "rows": self.rows}


@dataclass(slots=True)
class _Resource:
    service: str | None
    version: str | None
    instance: str | None
    region: str | None
    attrs: list[tuple[str, str | None]]
    records: int = 0
    first_ns: int | None = None
    last_ns: int | None = None

    def saw(self, t: int | None) -> None:
        self.records += 1
        if t:
            self.first_ns = t if self.first_ns is None else min(self.first_ns, t)
            self.last_ns = t if self.last_ns is None else max(self.last_ns, t)


def _resource(res: dict | None, seen: dict[str, _Resource]) -> tuple[str, _Resource]:
    attrs = sorted(_attrs((res or {}).get("attributes")), key=lambda kv: kv[0])
    rid = hashlib.sha256(orjson.dumps(attrs)).hexdigest()[:16]
    r = seen.get(rid)
    if r is None:
        a = dict(attrs)
        r = seen[rid] = _Resource(
            a.get("service.name"), a.get("service.version"), a.get("service.instance.id"), a.get("cloud.region"), attrs
        )
    return rid, r


def _seg_name(seg: SegmentEntry) -> str:
    return f"{seg.signal}-{seg.stream}-{seg.seq:06d}"


def build_segment(corpus: Path, seg: SegmentEntry, out: Path) -> BuiltSegment:
    name = _seg_name(seg)
    sinks = {}
    for t in TABLES_BY_SIGNAL[seg.signal]:
        (out / t).mkdir(parents=True, exist_ok=True)
        sinks[t] = _Sink(out / t / f"{name}.parquet", TABLES[t])
    seen: dict[str, _Resource] = {}
    archive = Archive(corpus)
    raw = 0
    for e, body in archive.bodies(seg):
        raw += len(body)
        doc = orjson.loads(body)
        if seg.signal == "traces":
            _spans(doc, sinks, seen, name, e.index)
        elif seg.signal == "logs":
            _logs(doc, sinks["logs"], seen, name, e.index)
        else:
            _points(doc, sinks["points"], seen, name, e.index)
    rs = sinks["resources"]
    for rid in sorted(seen):
        r = seen[rid]
        rs.add(rid, r.service, r.version, r.instance, r.region, r.attrs, r.records, r.first_ns, r.last_ns, name)
    outputs = {t: s.close() for t, s in sinks.items()}
    return BuiltSegment(seg.signal, seg.stream, seg.seq, seg.sha256, SCHEMA_VERSION, builder_sha256(), outputs, raw)


def _spans(doc: dict, sinks: dict[str, _Sink], seen: dict[str, _Resource], seg: str, frame: int) -> None:
    spans, events = sinks["spans"], sinks["span_events"]
    for rs in doc.get("resourceSpans") or ():
        rid, r = _resource(rs.get("resource"), seen)
        for ss in rs.get("scopeSpans") or ():
            scope = (ss.get("scope") or {}).get("name")
            for sp in ss.get("spans") or ():
                attrs = _attrs(sp.get("attributes"))
                a = dict(attrs)
                start, end = _ns(sp.get("startTimeUnixNano")), _ns(sp.get("endTimeUnixNano"))
                status = sp.get("status") or {}
                evs = sp.get("events") or ()
                r.saw(start)
                spans.add(
                    sp.get("traceId"),
                    sp.get("spanId"),
                    _hexid(sp.get("parentSpanId")),
                    r.service,
                    r.version,
                    r.instance,
                    r.region,
                    sp.get("name"),
                    _enum(sp.get("kind"), SPAN_KINDS, "SPAN_KIND_"),
                    start,
                    end,
                    end - start if start and end else None,
                    _enum(status.get("code"), STATUS_CODES, "STATUS_CODE_"),
                    status.get("message") or None,
                    _http_status(a),
                    a.get("http.route"),
                    _url_path(a),
                    len(evs),
                    attrs,
                    scope,
                    rid,
                    seg,
                    frame,
                )
                for ev in evs:
                    events.add(
                        sp.get("traceId"),
                        sp.get("spanId"),
                        r.service,
                        _ns(ev.get("timeUnixNano")),
                        ev.get("name"),
                        _attrs(ev.get("attributes")),
                        seg,
                        frame,
                    )


def _logs(doc: dict, sink: _Sink, seen: dict[str, _Resource], seg: str, frame: int) -> None:
    for rl in doc.get("resourceLogs") or ():
        rid, r = _resource(rl.get("resource"), seen)
        for sl in rl.get("scopeLogs") or ():
            scope = (sl.get("scope") or {}).get("name")
            for lr in sl.get("logRecords") or ():
                observed = _ns(lr.get("observedTimeUnixNano"))
                ts = _ns(lr.get("timeUnixNano")) or observed
                body = lr.get("body") or {}
                text = body["stringValue"] if "stringValue" in body else anyvalue_str(body)
                r.saw(ts)
                sink.add(
                    ts,
                    observed,
                    ts // MINUTE_NS if ts else None,
                    r.service,
                    r.version,
                    r.instance,
                    r.region,
                    _severity(lr.get("severityNumber")),
                    lr.get("severityText") or None,
                    text,
                    _hexid(lr.get("traceId")),
                    _hexid(lr.get("spanId")),
                    _attrs(lr.get("attributes")),
                    scope,
                    rid,
                    seg,
                    frame,
                )


def _points(doc: dict, sink: _Sink, seen: dict[str, _Resource], seg: str, frame: int) -> None:
    for rm in doc.get("resourceMetrics") or ():
        rid, r = _resource(rm.get("resource"), seen)
        for sm in rm.get("scopeMetrics") or ():
            scope = (sm.get("scope") or {}).get("name")
            for m in sm.get("metrics") or ():
                for key, typ in METRIC_TYPES.items():
                    data = m.get(key)
                    if not data:
                        continue
                    temporality = data.get("aggregationTemporality")
                    temporality = (
                        None
                        if typ == "gauge" or typ == "summary"
                        else _enum(temporality, TEMPORALITIES, "AGGREGATION_TEMPORALITY_")
                    )
                    monotonic = bool(data.get("isMonotonic")) if typ == "sum" else None
                    for p in data.get("dataPoints") or ():
                        ts = _ns(p.get("timeUnixNano"))
                        as_int = p.get("asInt")
                        value_int = int(as_int) if as_int is not None else None
                        value = (
                            float(p["asDouble"])
                            if "asDouble" in p
                            else (float(value_int) if value_int is not None else None)
                        )
                        r.saw(ts)
                        sink.add(
                            m.get("name"),
                            typ,
                            m.get("unit") or None,
                            monotonic,
                            temporality,
                            ts,
                            _ns(p.get("startTimeUnixNano")),
                            ts // MINUTE_NS if ts else None,
                            r.service,
                            r.version,
                            r.instance,
                            r.region,
                            _attrs(p.get("attributes")),
                            value,
                            value_int,
                            int(p["count"]) if "count" in p else None,
                            float(p["sum"]) if "sum" in p else None,
                            float(p["min"]) if "min" in p else None,
                            float(p["max"]) if "max" in p else None,
                            [float(b) for b in p.get("explicitBounds") or ()] if typ == "histogram" else None,
                            [int(c) for c in p.get("bucketCounts") or ()] if typ == "histogram" else None,
                            scope,
                            rid,
                            seg,
                            frame,
                        )


def read_built(out: Path) -> dict[str, BuiltSegment]:
    """Complete lines only: a build killed mid-write leaves a torn last line, which names no built segment."""
    path = out / "built.jsonl"
    if not path.exists():
        return {}
    data = path.read_bytes()
    done = {}
    for line in data[: data.rfind(b"\n") + 1].splitlines():
        if line:
            b = BuiltSegment(**orjson.loads(line))
            done[b.segment_sha256] = b
    return done


def _truncate_torn_tail(path: Path) -> None:
    if path.exists():
        data = path.read_bytes()
        end = data.rfind(b"\n") + 1
        if end != len(data):
            with path.open("r+b") as f:
                f.truncate(end)


def _intact(out: Path, b: BuiltSegment) -> bool:
    if b.builder_sha256 != builder_sha256() or b.schema_version != SCHEMA_VERSION:
        return False
    for table, o in b.outputs.items():
        p = out / table / o["path"]
        if not p.exists() or hashlib.sha256(p.read_bytes()).hexdigest() != o["sha256"]:
            return False
    return True


def ref_dir(corpus: Path) -> Path:
    return corpus / "derived" / "ref"


def build(corpus: Path, *, workers: int = 4) -> BuildReport:
    t0 = time.monotonic()
    archive = Archive.open(corpus, verify="index")
    out = ref_dir(corpus)
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob("*/*.tmp"):
        stale.unlink(missing_ok=True)
    _truncate_torn_tail(out / "built.jsonl")
    done = read_built(out)
    segs = archive.segments()
    todo = [s for s in segs if not (s.sha256 in done and _intact(out, done[s.sha256]))]
    with ProcessPoolExecutor(max_workers=workers) as pool, (out / "built.jsonl").open("ab") as ledger:
        futures = [pool.submit(build_segment, corpus, s, out) for s in todo]
        for f in as_completed(futures):
            ledger.write(orjson.dumps(asdict(f.result())) + b"\n")
            ledger.flush()
            os.fsync(ledger.fileno())
    built = read_built(out)
    rows = {t: 0 for t in TABLES}
    for s in segs:
        for t, o in built[s.sha256].outputs.items():
            rows[t] += o["rows"]
    seconds = time.monotonic() - t0
    raw = sum(built[s.sha256].raw_bytes for s in todo)
    report = BuildReport(
        segments_total=len(segs),
        segments_built=len(todo),
        segments_skipped=len(segs) - len(todo),
        rows=rows,
        seconds=round(seconds, 3),
        bytes_in=sum(s.bytes for s in todo),
        bytes_in_raw=raw,
        mb_per_s_raw=round(raw / seconds / 1e6, 2) if seconds else 0.0,
        peak_rss_parent_bytes=peak_rss_bytes(resource.RUSAGE_SELF),
        peak_rss_worker_bytes=peak_rss_bytes(resource.RUSAGE_CHILDREN),
        workers=workers,
        host=describe_host(),
    )
    (out / "build_report.json").write_bytes(orjson.dumps(asdict(report), option=orjson.OPT_INDENT_2) + b"\n")
    return report
