"""Name the backend defect behind each failing gate check, from evidence in the archive, never assumed.

  uv run python tests/live/gate_explain.py state/sessions/<id> [--out FILE]

Each rule tests the data the session sent; a failing check no rule explains is listed as UNEXPLAINED.
- B4 log counts per minute: the service's log records carry no timeUnixNano, Parseable stores the arrival time
  in time_unix_nano instead of leaving it empty, and the same count over a window covering whole frames passes.
- B4 metrics: the metric has exemplars on its points (dropped with HTTP 200), or the expected value is an integer
  point (asInt stored as NULL, ingest defect 4), or the stored double is one ulp from the sent one.
- Log counts and refusal counters after a kill: the surplus equals the records of an in-doubt frame.
- B1 span counts: the shortfall fits within the matching spans of traces whose frames arrived over more than
  Tempo's max_trace_live, which the live-store writes as several rows and search returns one of.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

import orjson

from otb.archive import Archive, FrameRef
from otb.ingest import Session, corpus_path, selected_frames
from otb.ref.store import ReferenceStore
from otb.stacks import metric_key

TIMELESS = "Parseable sets a missing log timeUnixNano to the arrival time (minute buckets shift; totals equal)"
EXEMPLARS = "Parseable drops every point of a metric whose points carry exemplars, with HTTP 200 (U4)"
INT_NULL = "Parseable stores integer (asInt) metric points as NULL (ingest defect 4)"
ULP = "Parseable returns the double one ulp away from the value sent"
DUPLICATE = "the surplus equals the records of a frame in doubt, resent after the feeder was killed: stored twice"
DOUBT_COUNTERS = "the collector counted an in-doubt frame refused when its client was killed mid-request (2 per record)"
TEMPO_FRAGMENT = (
    "Tempo search returns one fragment of a trace whose spans arrived over more than live_store.max_trace_live; "
    "the shortfall fits within the matching spans of such traces"
)
MAX_TRACE_LIVE_S = 60


def tempo_fragment(store: object, archive: Archive, cid: str, c: dict) -> str | None:
    """A B1 span-count shortfall no larger than the matching spans (or traces) of traces whose frames arrived
    over more than max_trace_live: those are the traces the live-store writes as several rows."""
    kind = cid.split(":", 1)[0]
    if kind not in ("CountSpans", "DistinctTraces") or not isinstance(c["got"], int) or c["got"] >= c["expected"]:
        return None
    check = json.loads(cid.split(":", 1)[1].rsplit("@", 1)[0])
    start, end = (int(x) for x in cid.rsplit("@", 1)[1].split("-"))
    where = ["start_ns >= ?", "start_ns < ?"]
    args: list = [start, end]
    if check.get("service") is not None:
        where.append("service = ?")
        args.append(check["service"])
    if check.get("errors_only"):
        where.append("status_code = 'ERROR'")
    if check.get("kind"):
        where.append("kind = ?")
        args.append(check["kind"])
    matched = store.con.execute(f"SELECT trace_id FROM spans WHERE {' AND '.join(where)}", args).fetchall()  # type: ignore[attr-defined]
    traces = {t for (t,) in matched}
    frames = store.con.execute(  # type: ignore[attr-defined]
        "SELECT DISTINCT trace_id, seg, frame FROM spans WHERE trace_id IN (SELECT unnest(?::VARCHAR[]))",
        [sorted(traces)],
    ).fetchall()
    dues: dict[str, list[int]] = defaultdict(list)
    for t, seg, fr in frames:
        signal, stream, seq = seg.split("-")
        dues[t].append(archive.index(signal, stream, int(seq))[fr].max_event_ns)  # type: ignore[arg-type]
    long = {t for t, d in dues.items() if (max(d) - min(d)) / 1e9 > MAX_TRACE_LIVE_S}
    room = len(long) if kind == "DistinctTraces" else sum(1 for (t,) in matched if t in long)
    return TEMPO_FRAGMENT if c["expected"] - c["got"] <= room else None


def doubt_records(session: Session, frames: list[str]) -> list[tuple[str, str | None, int, int]]:
    """(signal, service, event time, error flag) of every log record in the in-doubt frames (logs only: the one
    in-doubt frame check b produced is a logs frame)."""
    a = Archive(corpus_path(session))
    out = []
    for name in frames:
        sig_stream, idx = name.split("#")
        signal, stream, seq = sig_stream.split("/")
        body = orjson.loads(a.read(FrameRef(signal, stream, int(seq), int(idx))))  # type: ignore[arg-type]
        for rl in body.get("resourceLogs") or ():
            attrs = (rl.get("resource") or {}).get("attributes") or ()
            svc = next((x["value"].get("stringValue") for x in attrs if x["key"] == "service.name"), None)
            for sl in rl.get("scopeLogs") or ():
                for r in sl.get("logRecords") or ():
                    t = int(r.get("timeUnixNano") or 0) or int(r.get("observedTimeUnixNano") or 0)
                    err = int(r.get("severityNumber") or 0) >= 17 or str(r.get("severityText", "")).lower() in (
                        "error",
                        "fatal",
                    )
                    out.append(("logs", svc, t, int(err)))
    return out


def duplicate(cid: str, c: dict, doubt: list) -> str | None:
    kind = cid.split(":", 1)[0]
    if kind == "IngestCounters" and c["got"] and c["got"] == 2 * sum(1 for d in doubt if d[0] == c["signal"]):
        return DOUBT_COUNTERS
    if kind != "CountLogs" or not isinstance(c["got"], int):
        return None
    check = json.loads(cid.split(":", 1)[1].rsplit("@", 1)[0])
    start, end = (int(x) for x in cid.rsplit("@", 1)[1].split("-"))
    n = sum(
        1
        for sig, svc, t, err in doubt
        if sig == "logs"
        and start <= t < end
        and check["service"] in (None, svc)
        and (err or not check["error_only"])
        and check["contains"] is None
    )
    return DUPLICATE if n and c["got"] - c["expected"] == n else None


def scan(session: Session) -> tuple[dict[str, int], set[str]]:
    """Per service, log records without timeUnixNano; and metric names with an exemplar on any point."""
    a = Archive(corpus_path(session))
    timeless: dict[str, int] = defaultdict(int)
    with_exemplars: set[str] = set()
    for ref, e in selected_frames(a, session.window):
        if ref.signal == "metrics":
            body = orjson.loads(a.read(ref, e))
            for rm in body.get("resourceMetrics") or ():
                for sm in rm.get("scopeMetrics") or ():
                    for m in sm.get("metrics") or ():
                        data = next((v for k, v in m.items() if k in ("sum", "gauge", "histogram")), {})
                        if any(p.get("exemplars") for p in data.get("dataPoints") or ()):
                            with_exemplars.add(m["name"])
        elif ref.signal == "logs":
            body = orjson.loads(a.read(ref, e))
            for rl in body.get("resourceLogs") or ():
                attrs = (rl.get("resource") or {}).get("attributes") or ()
                svc = next((x["value"].get("stringValue") for x in attrs if x["key"] == "service.name"), None)
                for sl in rl.get("scopeLogs") or ():
                    for r in sl.get("logRecords") or ():
                        if not int(r.get("timeUnixNano") or 0):
                            timeless[str(svc)] += 1
    return timeless, with_exemplars


def explain(stack: str, cid: str, c: dict, totals_pass: dict, timeless: dict, exemplars: set) -> str:
    check = json.loads(cid.split(":", 1)[1].rsplit("@", 1)[0])
    kind = cid.split(":", 1)[0]
    if stack == "b4-parseable":
        if kind == "CountLogs":
            svcs = [check["service"]] if check["service"] else [s for s, n in timeless.items() if n]
            if all(timeless.get(s) for s in svcs) and totals_pass.get(check["service"]):
                return TIMELESS
        if kind in ("SeriesPoints", "SeriesLast") and check["metric"] in exemplars:
            return EXEMPLARS
        if kind == "SeriesLast" and isinstance(c["expected"], int) and c["got"] is None:
            return INT_NULL
        near = (
            (math.nextafter(c["expected"], math.inf), math.nextafter(c["expected"], -math.inf))
            if isinstance(c["expected"], float)
            else ()
        )
        if kind == "SeriesLast" and c["got"] in near:
            return ULP
        if kind == "MetricNames":
            missing, extra = set(c["expected"]) - set(c["got"]), set(c["got"]) - set(c["expected"])
            if not extra and missing <= {metric_key(m) for m in exemplars}:
                return EXEMPLARS
    return "UNEXPLAINED"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("session", type=Path)
    p.add_argument("--out", type=Path)
    a = p.parse_args()
    session = Session.load(a.session / "session.json")
    gate = json.loads((a.session / "gate" / "gate.json").read_text())
    timeless, exemplars = scan(session)
    out: dict = {"session": session.id, "timeless_log_records": dict(timeless), "stacks": {}}
    named = json.loads((a.session / "gate" / "plan.json").read_text())["windows"]
    longest = max((w for k, w in named.items() if not k.startswith("instance:")), key=lambda w: w[1] - w[0])
    archive = Archive(corpus_path(session))
    frames = None
    if session.window is not None:
        frames = [(f"{r.signal}-{r.stream}-{r.seq:06d}", r.index) for r, _ in selected_frames(archive, session.window)]
    store = ReferenceStore.open(corpus_path(session), frames=frames)
    for sid, v in gate["stacks"].items():
        totals_pass = {}
        for cid, c in v["checks"].items():
            if cid.startswith("CountLogs:") and cid.endswith(f"@{longest[0]}-{longest[1]}"):
                check = json.loads(cid.split(":", 1)[1].rsplit("@", 1)[0])
                if not check["error_only"] and check["contains"] is None:
                    totals_pass[check["service"]] = c["status"] == "pass"
        ingest = json.loads((a.session / "gate" / sid / "ingest.json").read_text())
        doubt = doubt_records(session, ingest["in_doubt"])
        named: dict[str, list[str]] = defaultdict(list)
        for cid, c in v["checks"].items():
            if c["status"] == "fail":
                cause = (
                    duplicate(cid, c, doubt)
                    or (tempo_fragment(store, archive, cid, c) if sid == "b1-lgtm" else None)
                    or explain(sid, cid, c, totals_pass, timeless, exemplars)
                )
                named[cause].append(cid)
        out["stacks"][sid] = {
            "fail": v["counts"]["fail"],
            "by_cause": {k: len(x) for k, x in named.items()},
            "unexplained": named.get("UNEXPLAINED", []),
            "longest_window_log_totals_pass": totals_pass,
        }
    text = json.dumps(out, indent=1) + "\n"
    if a.out:
        a.out.write_text(text)
    print(text)


if __name__ == "__main__":
    main()
