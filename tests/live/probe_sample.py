"""Check 3 of U4: load one shifted sample into every stack, then run every Check through every stack's probes and
compare with values computed from the sample alone.

  uv run python tests/live/probe_sample.py load  [--lines 60] [--stacks b1,b2,b3,b4]
  uv run python tests/live/probe_sample.py probe [--stacks ...] [--out docs/v1/units/U4-probes.json]

`load` POSTs each request to each stack's ingest collector and records the HTTP statuses; the sample's clock
(now_ns) is kept in .otb/state/sample.json so `probe` rebuilds byte-identical bodies and the same expected values.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import sample as S

from otb.stacks import (
    REPO,
    Check,
    CountLogs,
    CountSpans,
    DistinctTraces,
    Exact,
    IngestCounters,
    MaxAttrLen,
    MetricNames,
    SeriesLast,
    SeriesPoints,
    StackContext,
    TraceSpanCount,
    check_id,
    check_signal,
    metric_key,
    probe_json,
    registry,
    run_probe,
    select,
)
from otb.timebase import NS, WallWindow

STATE = REPO / ".otb" / "state" / "sample.json"
MIN = 60 * NS


def floor_min(t: int) -> int:
    return t // MIN * MIN


ARRIVAL = "Parseable filters on arrival time: the sample was POSTed after this window closed (plus max_late_s)"
EXEMPLARS = (
    "Parseable v3.2.3 drops every point of a metric in which any point carries exemplars, with HTTP 200 "
    "(tests/live/parseable_exemplars.py)"
)
INT_NULL = "Parseable ingest defect 4: integer (asInt) metric points stored as NULL"


def known_defect(
    stack: str, check: Check, w: WallWindow, want: object, got: object, r: S.Records, arrived_ns: int
) -> str | None:
    """Names a difference that a known backend defect explains, checked against the sample, not assumed.
    Anything else that differs is a failure of this check."""
    if stack != "b4-parseable":
        return None
    signal = check_signal(check)
    late = registry()[stack].max_late_s[signal] if signal else None
    if signal and arrived_ns > w.end + int((late or 0) * NS):
        return ARRIVAL if got in (0, None, []) else None
    match check:
        case SeriesPoints(metric=m, attrs=attrs) | SeriesLast(metric=m, attrs=attrs):
            pts = [p for p in r.points if p["metric"] == m and _in(p["t"], w) and _attrs_match(p["attrs"], attrs)]
            if any(p["exemplar"] for p in pts) and (got is None or (isinstance(got, int) and got < len(pts))):
                return EXEMPLARS
            if isinstance(check, SeriesLast) and got is None and isinstance(want, int):
                return INT_NULL
        case MetricNames():
            with_exemplars = {metric_key(p["metric"]) for p in r.points if p["exemplar"] and _in(p["t"], w)}
            missing, extra = set(want) - set(got), set(got) - set(want)  # type: ignore[arg-type]
            if not extra and missing <= with_exemplars:
                return EXEMPLARS
    return None


# ---------- expected values, from the sample alone ----------


def _in(t: int, w: WallWindow) -> bool:
    return w.start <= t < w.end


def _attrs_match(attrs: dict, want: tuple[tuple[str, str], ...]) -> bool:
    return all(k in attrs and str(attrs[k]) == v for k, v in want)


def expected(check: Check, w: WallWindow, r: S.Records) -> object:
    match check:
        case CountLogs(service=svc, error_only=err, contains=sub):
            return sum(
                1
                for x in r.logs
                if _in(x["t"], w)
                and (svc is None or x["service"] == svc)
                and (not err or x["sevnum"] >= 17 or x["sevtext"] in ("error", "fatal"))
                and (sub is None or sub in x["body"])
            )
        case CountSpans(service=svc, errors_only=err, kind=kind):
            k = {"SERVER": (2, "SPAN_KIND_SERVER"), "CLIENT": (3, "SPAN_KIND_CLIENT")}.get(kind or "", ())
            return sum(
                1
                for s in r.spans
                if _in(s["t"], w)
                and (svc is None or s["service"] == svc)
                and (not err or s["error"])
                and (kind is None or s["kind"] in k)
            )
        case TraceSpanCount(trace_id=tid):
            return sum(1 for s in r.spans if s["trace_id"] == tid and _in(s["t"], w))
        case DistinctTraces(service=svc):
            return len({s["trace_id"] for s in r.spans if _in(s["t"], w) and (svc is None or s["service"] == svc)})
        case SeriesPoints(metric=m, attrs=attrs):
            return sum(1 for p in r.points if p["metric"] == m and _in(p["t"], w) and _attrs_match(p["attrs"], attrs))
        case SeriesLast(metric=m, attrs=attrs):
            pts = [
                (p["t"], p["value"])
                for p in r.points
                if p["metric"] == m and _in(p["t"], w) and _attrs_match(p["attrs"], attrs)
            ]
            return max(pts)[1] if pts else None
        case MetricNames():
            return sorted({metric_key(p["metric"]) for p in r.points if _in(p["t"], w)})
        case MaxAttrLen(signal=sig, key=key):
            rows = r.spans if sig == "traces" else r.logs
            lens = [len(x["attrs"][key]) for x in rows if _in(x["t"], w) and isinstance(x["attrs"].get(key), str)]
            return max(lens) if lens else None
        case IngestCounters():
            return {"traces": 0, "logs": 0, "metrics": 0}
    raise AssertionError(check)


# ---------- the check plan, derived deterministically from the sample ----------


def plan(r: S.Records, posted_ns: int) -> tuple[dict[str, WallWindow], list[tuple[str, Check]]]:
    t0 = min(x["t"] for x in r.spans + r.logs + r.points)
    full = WallWindow(floor_min(t0), floor_min(posted_ns) + 2 * MIN)  # type: ignore[arg-type]
    mid = sorted(s["t"] for s in r.spans)[len(r.spans) // 2]
    minute = WallWindow(floor_min(mid), floor_min(mid) + MIN)  # type: ignore[arg-type]
    windows = {"full": full, "minute": minute}

    log_svc = [s for s, _ in Counter(x["service"] for x in r.logs).most_common(3)]
    err_log_svc = [
        s
        for s, _ in Counter(
            x["service"] for x in r.logs if x["sevnum"] >= 17 or x["sevtext"] in ("error", "fatal")
        ).most_common(3)
    ]
    words = Counter(wd for x in r.logs for wd in x["body"].split() if len(wd) >= 8 and wd.isalnum())
    token = words.most_common(1)[0][0]
    span_svc = [s for s, _ in Counter(s["service"] for s in r.spans).most_common(2)]
    err_span_svc = [s for s, _ in Counter(s["service"] for s in r.spans if s["error"]).most_common(3)]
    per_trace = Counter(s["trace_id"] for s in r.spans)
    by_size = sorted(per_trace, key=lambda t: (per_trace[t], t))
    err_trace = sorted({s["trace_id"] for s in r.spans if s["error"]})[0]
    traces = [by_size[-1], err_trace, by_size[len(by_size) // 2]]

    checks: list[tuple[str, Check]] = [("full", IngestCounters())]
    checks += [("full", CountLogs(None, False)), ("full", CountLogs(None, True)), ("minute", CountLogs(None, False))]
    checks += [("full", CountLogs(s, False)) for s in log_svc]
    checks += [("full", CountLogs(s, True)) for s in err_log_svc]
    checks += [("full", CountLogs(None, False, token))]
    checks += [("full", CountSpans(None, False)), ("full", CountSpans(None, True)), ("minute", CountSpans(None, False))]
    checks += [("full", CountSpans(s, True)) for s in err_span_svc]
    checks += [("full", CountSpans(span_svc[0], False, "SERVER")), ("full", CountSpans(span_svc[0], False, "CLIENT"))]
    checks += [("full", TraceSpanCount(t)) for t in traces] + [("minute", TraceSpanCount(traces[0]))]
    checks += [("full", DistinctTraces(None)), ("full", DistinctTraces(span_svc[0])), ("minute", DistinctTraces(None))]

    series = [
        pick_series(r, lambda p: p["kind"] == "sum" and p["monotonic"] and isinstance(p["value"], int), "service.name"),
        pick_series(r, lambda p: p["kind"] == "gauge" and isinstance(p["value"], float), "service.name"),
        pick_series(r, lambda p: p["kind"] == "histogram", "service.name"),
        pick_series(r, lambda p: p["kind"] == "sum" and p["monotonic"], "container.name"),
    ]
    checks += [("full", SeriesPoints(m, a)) for m, a in series] + [("minute", SeriesPoints(*series[0]))]
    checks += [("full", SeriesLast(m, a)) for m, a in (series[0], series[1], series[3])]
    checks += [("full", MetricNames()), ("minute", MetricNames())]

    for sig, rows in (("traces", r.spans), ("logs", r.logs)):
        lens = Counter()
        for x in rows:
            for k, v in x["attrs"].items():
                if isinstance(v, str):
                    lens[k] = max(lens[k], len(v))
        key = sorted(lens, key=lambda k: (-lens[k], k))[0]
        checks.append(("full", MaxAttrLen(sig, key)))  # type: ignore[arg-type]
    return windows, checks


def pick_series(r: S.Records, pred, anchor: str) -> tuple[str, tuple[tuple[str, str], ...]]:
    """The series with most points among points matching pred and carrying the anchor attribute; its attrs are
    the anchor plus every string-valued data-point attribute, so the selection names exactly one series."""
    c: Counter[tuple[str, tuple[tuple[str, str], ...]]] = Counter()
    for p in r.points:
        if pred(p) and anchor in p["attrs"]:
            keys = sorted(k for k in p["point_attrs"] if isinstance(p["attrs"][k], str))
            attrs = ((anchor, str(p["attrs"][anchor])), *((k, p["attrs"][k]) for k in keys))
            c[(p["metric"], attrs)] += 1
    return sorted(c, key=lambda k: (-c[k], k))[0]


# ---------- commands ----------


def cmd_load(args: argparse.Namespace) -> None:
    now_ns = time.time_ns()
    sample = S.build(args.lines, now_ns)
    stacks = select(registry(), args.stacks)
    statuses: dict[str, dict[str, Counter]] = {s.id: {} for s in stacks}
    for signal in ("metrics", "traces", "logs"):
        for body in sample.bodies[signal]:
            for s in stacks:
                code, text = S.post(s.ingest.url, signal, body)
                statuses[s.id].setdefault(signal, Counter())[f"{code} {text[:120]}"] += 1
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(
        json.dumps(
            {
                "lines": args.lines,
                "now_ns": now_ns,
                "posted_ns": time.time_ns(),
                "stacks": [s.id for s in stacks],
                "post": statuses,
            },
            indent=1,
        )
    )
    print(json.dumps({"stats": S.stats(sample), "post": statuses}, indent=1))


def cmd_probe(args: argparse.Namespace) -> None:
    st = json.loads(STATE.read_text())
    wait_s = st["posted_ns"] / NS + args.settle_s - time.time()
    if wait_s > 0:
        print(f"settling {wait_s:.0f}s after the load before probing")
        time.sleep(wait_s)
    sample = S.build(st["lines"], st["now_ns"])
    recs = S.records(sample)
    windows, checks = plan(recs, st["posted_ns"])
    stacks = select(registry(), args.stacks)
    out: dict[str, object] = {
        "unit": "U4 check 3",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sample": S.stats(sample) | {"lines_per_file": st["lines"], "source": str(S.RECORDING)},
        "windows": {k: {"start_ns": w.start, "end_ns": w.end} for k, w in windows.items()},
        "post": st["post"],
        "probed_after_load_s": round(time.time() - st["posted_ns"] / NS, 1),
        "stacks": {},
    }
    summary: dict[str, Counter] = {}
    for s in stacks:
        ctx = StackContext(s)
        rows = []
        for wname, check in checks:
            w = windows[wname]
            want = expected(check, w, recs)
            t0 = time.monotonic()
            got = run_probe(s, check, w, ctx)
            row = {"check": check_id(check), "window": wname, "expected": want} | probe_json(got)
            row["secs"] = round(time.monotonic() - t0, 3)
            if isinstance(got, Exact):
                row["match"] = got.value == want
                defect = known_defect(s.id, check, w, want, got.value, recs, st["now_ns"])
                row["defect"] = None if row["match"] else defect
                verdict = (
                    "exact_match" if row["match"] else ("exact_known_defect" if row["defect"] else "exact_MISMATCH")
                )
            else:
                verdict = row["result"]
            row["verdict"] = verdict
            summary.setdefault(s.id, Counter())[verdict] += 1
            rows.append(row)
            got_text = str(row.get("value", row.get("approximate", row.get("error"))))[:80]
            detail = "" if verdict == "exact_match" else f"  want={str(want)[:60]} got={got_text}"
            print(f"{s.id:14s} {wname:6s} {verdict:20s} {check_id(check)[:90]}{detail}")
        out["stacks"][s.id] = rows  # type: ignore[index]
    out["summary"] = {k: dict(v) for k, v in summary.items()}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=1, sort_keys=False) + "\n")
    print(json.dumps(out["summary"], indent=1))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["load", "probe"])
    p.add_argument("--lines", type=int, default=60)
    p.add_argument("--stacks", default=None)
    p.add_argument("--settle-s", type=float, default=120, help="probe no earlier than this long after the load")
    p.add_argument("--out", default=str(REPO / "docs" / "v1" / "units" / "U4-probes.json"))
    a = p.parse_args()
    {"load": cmd_load, "probe": cmd_probe}[a.cmd](a)


if __name__ == "__main__":
    main()
