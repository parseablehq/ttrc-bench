"""Flatten one demo instance's raw OTLP/JSON recording into a DuckDB file that blind reviewers query: spans, logs
and metric points of that instance, one row each, with no derived columns beyond what the record states. Shares no
code with otb.

    python k8s/gke/r2b_flat.py <recording dir with {traces,logs,metrics}.jsonl.zst> <out .duckdb> <region>
"""

from __future__ import annotations

import io
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import duckdb
import zstandard

SKIP = {"flagd", "flagd-ui"}


def value(v: dict | None):
    if not v:
        return None
    for k in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if k in v:
            return int(v[k]) if k == "intValue" else v[k]
    return json.dumps(v, sort_keys=True)


def attrs(a) -> dict:
    return {x["key"]: value(x.get("value")) for x in a or ()}


def lines(path: Path):
    with path.open("rb") as f, zstandard.ZstdDecompressor().stream_reader(f) as r:
        for line in io.TextIOWrapper(r, encoding="utf-8"):
            if line.strip():
                yield json.loads(line)


def resources(doc: dict, key: str, region: str):
    for rs in doc.get(key, []):
        res = attrs(rs.get("resource", {}).get("attributes"))
        if res.get("cloud.region") == region and res.get("service.name") not in SKIP:
            yield res, rs


def spans(src: Path, out: Path, region: str) -> int:
    n = 0
    with out.open("w") as o:
        for doc in lines(src):
            for res, rs in resources(doc, "resourceSpans", region):
                for ss in rs.get("scopeSpans", []):
                    for s in ss.get("spans", []):
                        a, st = attrs(s.get("attributes")), s.get("status") or {}
                        row = {
                            "trace_id": s["traceId"],
                            "span_id": s["spanId"],
                            "parent_span_id": s.get("parentSpanId") or None,
                            "service": res.get("service.name"),
                            "service_version": res.get("service.version"),
                            "name": s["name"],
                            "kind": s.get("kind", 0),
                            "start_ns": int(s["startTimeUnixNano"]),
                            "end_ns": int(s["endTimeUnixNano"]),
                            "status_code": st.get("code", 0),
                            "status_message": st.get("message") or None,
                            "attributes": json.dumps(a, sort_keys=True),
                        }
                        o.write(json.dumps(row) + "\n")
                        n += 1
    return n


def logs(src: Path, out: Path, region: str) -> int:
    n = 0
    with out.open("w") as o:
        for doc in lines(src):
            for res, rl in resources(doc, "resourceLogs", region):
                for sl in rl.get("scopeLogs", []):
                    for r in sl.get("logRecords", []):
                        row = {
                            "time_ns": int(r.get("timeUnixNano") or 0) or None,
                            "observed_time_ns": int(r.get("observedTimeUnixNano") or 0) or None,
                            "service": res.get("service.name"),
                            "scope": (sl.get("scope") or {}).get("name"),
                            "severity_number": r.get("severityNumber"),
                            "severity_text": r.get("severityText"),
                            "body": None if r.get("body") is None else str(value(r.get("body"))),
                            "trace_id": r.get("traceId") or None,
                            "span_id": r.get("spanId") or None,
                            "attributes": json.dumps(attrs(r.get("attributes")), sort_keys=True),
                        }
                        o.write(json.dumps(row) + "\n")
                        n += 1
    return n


def metrics(src: Path, out: Path, region: str) -> int:
    n = 0
    with out.open("w") as o:
        for doc in lines(src):
            for res, rm in resources(doc, "resourceMetrics", region):
                for sm in rm.get("scopeMetrics", []):
                    for m in sm.get("metrics", []):
                        for kind in ("sum", "gauge", "histogram", "exponentialHistogram", "summary"):
                            body = m.get(kind)
                            if not body:
                                continue
                            for p in body.get("dataPoints", []):
                                v = p.get("asInt", p.get("asDouble", p.get("sum")))
                                row = {
                                    "metric": m["name"],
                                    "kind": kind,
                                    "temporality": body.get("aggregationTemporality"),
                                    "monotonic": body.get("isMonotonic"),
                                    "service": res.get("service.name"),
                                    "resource": json.dumps(res, sort_keys=True),
                                    "time_ns": int(p.get("timeUnixNano") or 0),
                                    "start_time_ns": int(p.get("startTimeUnixNano") or 0) or None,
                                    "value": None if v is None else float(v),
                                    "count": None if p.get("count") is None else int(p["count"]),
                                    "attributes": json.dumps(attrs(p.get("attributes")), sort_keys=True),
                                }
                                o.write(json.dumps(row) + "\n")
                                n += 1
    return n


def job(args):
    fn, src, out, region = args
    return out.stem, globals()[fn](src, out, region)


def main(rec: Path, db: Path, region: str) -> int:
    tmp = db.parent / f".{db.stem}-flat"
    tmp.mkdir(parents=True, exist_ok=True)
    jobs = [
        (t, rec / f"{s}.jsonl.zst", tmp / f"{t}.jsonl", region)
        for t, s in (("spans", "traces"), ("logs", "logs"), ("metrics", "metrics"))
    ]
    with ProcessPoolExecutor(3) as pool:
        counts = dict(pool.map(job, jobs))
    con = duckdb.connect(str(db))
    for t in counts:
        src = tmp / f"{t}.jsonl"
        read = f"read_json('{src}', format='newline_delimited', sample_size=-1)"
        con.execute(f"CREATE OR REPLACE TABLE {t} AS SELECT * FROM {read}")
        got = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        if got != counts[t]:
            print(f"{t}: wrote {counts[t]} rows, loaded {got}", file=sys.stderr)
            return 1
        (tmp / f"{t}.jsonl").unlink()
    tmp.rmdir()
    con.close()
    print(json.dumps(counts))
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]))
