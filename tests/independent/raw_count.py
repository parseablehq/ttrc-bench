"""Independent count over raw v2 OTLP/JSON recordings (one export request per line).

Shares no code with otb: stdlib json only. Prints per signal: lines, records, distinct line bodies,
and how often each normaliser rule would fire, so the archive's totals can be checked against it. `records`
counts what normaliser n2 keeps: no flagd or flagd-ui resource, no metric named feature_flag. `records_raw`
counts every record in the recording.

  python tests/independent/raw_count.py <recording dir> [--from-ns N --to-ns N]

With a window, records are counted by the v2 answer key's rule (span start, log time else observed time,
point time) over the half-open window.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

METRIC_KINDS = ("sum", "gauge", "histogram", "exponentialHistogram", "summary")


FLAG_SERVICES = {"flagd", "flagd-ui"}


def flag_keys(items: list) -> int:
    return sum("feature_flag" in a.get("key", "") for a in items or [])


def service(resource: dict) -> str | None:
    for a in resource.get("attributes", []):
        if a["key"] == "service.name":
            return a["value"].get("stringValue")
    return None


def count(path: Path, signal: str, lo: int | None, hi: int | None) -> dict:
    out = {"lines": 0, "records": 0, "records_raw": 0, "distinct_lines": 0, "rr_web": 0, "empty_attr": 0}
    out |= {"null_body": 0, "flag_service_records": 0, "flag_events": 0, "flag_metric_points": 0, "flag_attrs": 0}
    seen = set()

    def inside(t: int) -> int:
        return int(lo is None or lo <= t < hi)

    with path.open("rb") as f:
        for raw in f:
            if not raw.strip():
                continue
            out["lines"] += 1
            seen.add(hashlib.sha256(raw.rstrip(b"\n")).digest())
            doc = json.loads(raw)
            for r in doc.get("resourceSpans", []) + doc.get("resourceLogs", []) + doc.get("resourceMetrics", []):
                flagd = service(r.get("resource", {})) in FLAG_SERVICES
                if not flagd:
                    out["flag_attrs"] += flag_keys(r.get("resource", {}).get("attributes"))
                for scope in r.get("scopeSpans", []) + r.get("scopeLogs", []) + r.get("scopeMetrics", []):
                    if not flagd:
                        out["flag_attrs"] += flag_keys(scope.get("scope", {}).get("attributes"))
                    rrweb = scope.get("scope", {}).get("name") == "rum.rr-web"
                    for sp in scope.get("spans", []):
                        n = inside(int(sp["startTimeUnixNano"]))
                        out["records_raw"] += n
                        if flagd:
                            out["flag_service_records"] += 1
                            continue
                        out["records"] += n
                        out["flag_attrs"] += flag_keys(sp.get("attributes"))
                        out["flag_attrs"] += sum(flag_keys(link.get("attributes")) for link in sp.get("links", []))
                        for ev in sp.get("events", []):
                            if "feature_flag" in ev.get("name", ""):
                                out["flag_events"] += 1
                            else:
                                out["flag_attrs"] += flag_keys(ev.get("attributes"))
                    for lr in scope.get("logRecords", []):
                        if rrweb:
                            out["rr_web"] += 1
                            continue
                        n = inside(int(lr.get("timeUnixNano") or 0) or int(lr.get("observedTimeUnixNano") or 0))
                        out["records_raw"] += n
                        if flagd:
                            out["flag_service_records"] += 1
                            continue
                        out["records"] += n
                        out["empty_attr"] += sum(not a.get("value") for a in lr.get("attributes", []))
                        out["flag_attrs"] += flag_keys([a for a in lr.get("attributes", []) if a.get("value")])
                        out["null_body"] += not lr.get("body")
                    for m in scope.get("metrics", []):
                        flag_metric = "feature_flag" in m.get("name", "")
                        for kind in METRIC_KINDS:
                            for pt in m.get(kind, {}).get("dataPoints", []):
                                n = inside(int(pt.get("timeUnixNano") or 0))
                                out["records_raw"] += n
                                if flagd:
                                    out["flag_service_records"] += 1
                                elif flag_metric:
                                    out["flag_metric_points"] += 1
                                else:
                                    out["records"] += n
                                    out["flag_attrs"] += flag_keys(pt.get("attributes"))
                                    out["flag_attrs"] += sum(
                                        flag_keys(ex.get("filteredAttributes")) for ex in pt.get("exemplars", [])
                                    )
    out["distinct_lines"] = len(seen)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("recording", type=Path)
    ap.add_argument("--from-ns", type=int)
    ap.add_argument("--to-ns", type=int)
    a = ap.parse_args()
    result = {s: count(a.recording / f"{s}.jsonl", s, a.from_ns, a.to_ns) for s in ("traces", "logs", "metrics")}
    result["total_lines"] = sum(result[s]["lines"] for s in ("traces", "logs", "metrics"))
    json.dump(result, sys.stdout, indent=1, sort_keys=True)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
