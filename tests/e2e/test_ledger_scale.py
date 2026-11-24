"""Resuming and reporting a ledger of millions of lines in bounded memory (tests/e2e/synthetic_ledger.py).

A 72-hour follow session writes about 4 M frames per stack, two ledger lines each. The feeder (Ledger) and the
report (stack_report, which `otb ingest status` and the gate read) used to load every line into Python objects:
measured 3.1 GB and 3.8 GB on 1.2 M frames, so four feeders resuming together needed more than the runner holds.

Ways the streamed fold can fail, each asserted below:
- peak RSS still grows with the line count instead of the frame count (the stated bound: 64 MB plus 160 bytes a
  frame, against the 1.5 KB a line the old path took);
- the report differs from the old implementation (kept here verbatim as the reference) on a ledger with every
  shape the feeder writes: a frame in doubt across a feeder restart, a transport error in doubt followed by an
  outcome, a rejection, retries with and without a status, late frames, a second outcome line for one frame,
  time strings left, and a torn last line;
- the large report names the wrong frames: the rejected, in-doubt and late lists and the per-signal counts are
  checked against the positions the generator chose;
- the localised frame spans (frame_spans) or the measured query horizon (query_horizon_s) differ from the
  reference, on a ledger with rejected and in-doubt frames, on one missing fewer frames than the cap (listed) and
  on one missing more (an empty list), or listing them lets memory follow the frames missing.

Evidence: tests/e2e/artifacts/ledger_scale.json (counts) and ledger_scale.timing.json (peak RSS and seconds).
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import orjson
from conftest import write_artifact
from synthetic_ledger import SIGNALS, T0_NS, write_index_only_archive, write_ledger

from otb.archive import Archive, FrameEntry, FrameRef
from otb.ingest import (
    LOCALISED_KINDS,
    MAX_LOCALISED_FRAMES,
    Session,
    SignalReport,
    StackReport,
    frame_name,
    horizon_of,
    selected_frames,
    stack_report,
)
from otb.timebase import NS

REPO = Path(__file__).resolve().parents[2]
BOUNDS = {"traces": 10.0, "logs": 10.0, "metrics": 10.0}
BASE_MB = 64
BYTES_PER_FRAME = 160

CHILD = """
import json, sys, time
from pathlib import Path
from otb.archive import Archive
from otb.host import peak_rss_bytes
from otb.ingest import Ledger, Session, stack_report
what, corpus, ledger, out = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4])
t0 = time.monotonic()


def peak():
    # On Linux ru_maxrss survives fork and exec, so a child started by a large pytest process reports the
    # parent's peak. VmHWM belongs to the address space this exec created.
    if sys.platform.startswith("linux"):
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) * 1024
    return peak_rss_bytes()


if what == "ledger":
    l = Ledger(ledger)
    l.close()
else:
    s = Session("s", str(corpus), "x", "replay", 0, 0, None, ("b",), "2026-01-01T00:00:00Z", None)
    r = stack_report(Archive(corpus), s, "b", json.loads(sys.argv[5]), ledger)
    out.write_text(json.dumps(r.to_json(), sort_keys=True))
print(json.dumps({"peak_rss_mb": round(peak() / 1e6, 1), "s": round(time.monotonic() - t0, 1)}))
"""


def session_for(corpus: Path) -> Session:
    return Session("s", str(corpus), "x", "replay", 0, 0, None, ("b",), "2026-01-01T00:00:00Z", None)


def measure(what: str, corpus: Path, ledger: Path, out: Path) -> dict:
    p = subprocess.run(
        [sys.executable, "-c", CHILD, what, str(corpus), str(ledger), str(out), json.dumps(BOUNDS)],
        cwd=REPO,
        capture_output=True,
        text=True,
        env=os.environ | {"PYTHONPATH": str(REPO)},
        timeout=600,
    )
    assert p.returncode == 0, p.stderr[-3000:]
    return json.loads(p.stdout)


def test_a_multi_million_line_ledger_folds_in_bounded_memory(tmp_path: Path, artifacts: Path) -> None:
    frames = int(os.environ.get("OTB_LEDGER_FRAMES", "400000"))  # per signal
    corpus, ledger = tmp_path / "corpus", tmp_path / "b.ledger.jsonl"
    write_index_only_archive(corpus, frames)
    special = write_ledger(ledger, frames)
    lines = sum(1 for _ in ledger.open("rb"))
    bound_mb = BASE_MB + BYTES_PER_FRAME * 3 * frames / 1e6
    resume = measure("ledger", corpus, ledger, tmp_path / "none")
    report = measure("report", corpus, ledger, tmp_path / "report.json")
    got = json.loads((tmp_path / "report.json").read_text())

    names = {k: [frame_name(r) for r in sorted({FrameRef(*f) for f in v})] for k, v in special.items()}
    listed = {k: {s: [x["frame"] for x in got["frame_spans"][k][s]] for s in SIGNALS} for k in LOCALISED_KINDS}
    result = {
        "frames": 3 * frames,
        "ledger_lines": lines,
        "ledger_mb": round(ledger.stat().st_size / 1e6),
        "finished": got["finished"],
        "complete": got["complete"],
        "rejected_frames": [r["frame"] for r in got["rejected"]],
        "in_doubt": got["in_doubt"],
        "late_first_20": got["late"],
        "signals": {
            s: {k: got["signals"][s][k] for k in ("frames_expected", "frames_ok", "frames_rejected", "late_frames")}
            for s in SIGNALS
        },
        "frame_spans_listed": {k: {s: len(listed[k][s]) for s in SIGNALS} for k in LOCALISED_KINDS},
        "arrival_horizon_s": got["arrival_horizon_s"],
        "query_horizon_s": got["query_horizon_s"],
    }
    write_artifact(
        artifacts / "ledger_scale.json",
        {k: v for k, v in result.items() if k not in ("rejected_frames", "in_doubt", "late_first_20")}
        | {"lists_match_generator": result["rejected_frames"] == names["rejected"]},
        timing={
            "resume_peak_rss_mb": resume["peak_rss_mb"],
            "resume_s": resume["s"],
            "report_peak_rss_mb": report["peak_rss_mb"],
            "report_s": report["s"],
            "bound_mb": round(bound_mb),
        },
    )

    assert lines > 2_000_000, "the ledger must be of the size the capture writes"
    assert resume["peak_rss_mb"] < bound_mb, f"resume peaked at {resume['peak_rss_mb']} MB"
    assert report["peak_rss_mb"] < bound_mb, f"report peaked at {report['peak_rss_mb']} MB"
    assert result["rejected_frames"] == names["rejected"]
    assert result["in_doubt"] == names["in_doubt"]
    for s in SIGNALS:
        late = [n for n in names["late"] if n.startswith(s + "/") and n not in names["rejected"]]  # late: ok and late
        assert result["late_first_20"][s] == late[:20]
        n_rej = sum(1 for n in names["rejected"] if n.startswith(s + "/"))
        assert result["signals"][s] == {
            "frames_expected": frames,
            "frames_ok": frames - n_rej,
            "frames_rejected": n_rej,
            "late_frames": len(late),
        }
    assert (result["finished"], result["complete"]) == (True, False)
    for kind in ("rejected", "in_doubt"):
        for s in SIGNALS:
            assert listed[kind][s] == [n for n in names[kind] if n.startswith(s + "/")], (kind, s)
    assert result["frame_spans_listed"]["missing"] == dict.fromkeys(SIGNALS, 0)
    assert (result["arrival_horizon_s"], result["query_horizon_s"]) == (10, 34), (
        "a 10 s frame span, plus the latest an ok frame was sent: 20 + 4 s"
    )


def test_the_streamed_report_equals_the_reference_on_every_ledger_shape(tmp_path: Path) -> None:
    corpus, ledger = tmp_path / "corpus", tmp_path / "b.ledger.jsonl"
    write_index_only_archive(corpus, 2000)
    special = write_ledger(ledger, 2000)
    assert all(special.values()), "every shape must occur in the small ledger"
    got = stack_report(Archive(corpus), session_for(corpus), "b", BOUNDS, ledger)
    want = reference_report(Archive(corpus), session_for(corpus), "b", BOUNDS, ledger)
    assert asdict(got) == asdict(want)
    assert got.signals["logs"].time_strings_left == sum(k % 3 for k in range(2000) if k % 97 == 0 and k % 1201 != 23)
    assert got.signals["traces"].late_max_s == 22.0, "frame 7 was sent 20 + 7 % 5 seconds late"
    assert len(got.in_doubt) == len({tuple(f) for f in special["in_doubt"]})
    end_23, end_1224 = T0_NS + 2_300_000_000, T0_NS + 122_400_000_000  # due every 100 ms
    assert got.frame_spans["rejected"]["traces"] == [
        {"frame": "traces/g0/000000#23", "start_ns": end_23 - 10 * NS, "end_ns": end_23 + 1},
        {"frame": "traces/g0/000002#224", "start_ns": end_1224 - 10 * NS, "end_ns": end_1224 + 1},
    ], "frames 23 and 1224 were rejected; a span is half-open, one nanosecond past the newest record"
    assert [x["frame"] for x in got.frame_spans["in_doubt"]["logs"]] == [
        frame_name(r) for r in sorted({FrameRef(*f) for f in special["in_doubt"]}) if r.signal == "logs"
    ]
    assert got.frame_spans["missing"] == {s: [] for s in SIGNALS}
    assert got.query_horizon_s == 32, "a 10 s frame span plus the 22 s frame 7 was sent late"

    for missing, listed in ((MAX_LOCALISED_FRAMES, True), (MAX_LOCALISED_FRAMES + 1, False)):
        corpus = tmp_path / f"corpus-missing-{missing}"
        write_index_only_archive(corpus, 2000 + missing)
        got = stack_report(Archive(corpus), session_for(corpus), "b", BOUNDS, ledger)
        want = reference_report(Archive(corpus), session_for(corpus), "b", BOUNDS, ledger)
        assert asdict(got) == asdict(want)
        assert all(v.frames_missing == missing for v in got.signals.values())
        assert [len(got.frame_spans["missing"][s]) for s in SIGNALS] == [missing if listed else 0] * 3


# ---------- the implementation before the streamed fold, verbatim: the reference the new path must equal ----------


def _ref(v: list) -> FrameRef:
    return FrameRef(v[0], v[1], int(v[2]), int(v[3]))


def reference_read_ledger(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = path.read_bytes()
    return [orjson.loads(line) for line in data[: data.rfind(b"\n") + 1].splitlines() if line]


def reference_frame_span(ref: FrameRef, e: FrameEntry) -> dict:
    return {"frame": frame_name(ref), "start_ns": e.min_event_ns, "end_ns": e.max_event_ns + 1}


def _quantile(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(q * len(xs)))], 3)


def reference_report(
    archive: Archive, session: Session, stack: str, max_late_s: dict[str, float | None], ledger_path: Path
) -> StackReport:
    expected: dict[str, list[int]] = {s: [0, 0] for s in SIGNALS}
    entries: dict[FrameRef, FrameEntry] = {}
    for ref, e in selected_frames(archive, session.window):
        expected[ref.signal][0] += 1
        expected[ref.signal][1] += e.records
        entries[ref] = e
    outcomes: dict[FrameRef, dict] = {}
    retries: dict[str, int] = dict.fromkeys(SIGNALS, 0)
    refused: dict[str, int] = dict.fromkeys(SIGNALS, 0)
    open_intents: set[FrameRef] = set()
    doubt: set[FrameRef] = set()
    for line in reference_read_ledger(ledger_path):
        ev = line["ev"]
        if ev == "start":
            doubt |= open_intents
            open_intents = set()
        elif ev == "intent":
            open_intents.add(_ref(line["f"]))
        elif ev == "retry":
            retries[line["f"][0]] += 1
            if line["http"] is not None and line["http"] >= 500:
                refused[line["f"][0]] += line["records"]
            if line.get("in_doubt"):
                doubt.add(_ref(line["f"]))
        elif ev == "outcome":
            ref = _ref(line["f"])
            outcomes[ref] = line
            open_intents.discard(ref)
    doubt |= open_intents
    signals: dict[str, SignalReport] = {}
    late: dict[str, list[str]] = {}
    late_spans: dict[str, list[dict]] = {}
    frame_spans: dict[str, dict[str, list[dict]]] = {k: {} for k in LOCALISED_KINDS}
    rejected = []
    reaches: list[int] = []
    for s in SIGNALS:
        mine = {r: o for r, o in outcomes.items() if r.signal == s}
        ok = [o for o in mine.values() if o["status"] == "ok"]
        lates = [(o["sent_ns"] - o["due_ns"]) / NS for o in ok]
        reaches += [
            entries[r].max_event_ns - entries[r].min_event_ns + max(0, o["sent_ns"] - o["due_ns"])
            for r, o in mine.items()
            if o["status"] == "ok" and r in entries and entries[r].min_event_ns
        ]
        by_kind = {
            "rejected": [r for r, o in mine.items() if o["status"] == "rejected"],
            "missing": [r for r in entries if r.signal == s and r not in mine],
            "in_doubt": [r for r in doubt if r.signal == s],
        }
        for kind, refs in by_kind.items():
            spans = [reference_frame_span(r, entries[r]) for r in sorted(refs) if r in entries]
            frame_spans[kind][s] = spans if len(spans) <= MAX_LOCALISED_FRAMES else []
        bound = max_late_s.get(s)
        late_refs = sorted(
            r
            for r, o in mine.items()
            if o["status"] == "ok" and bound is not None and (o["sent_ns"] - o["due_ns"]) / NS > bound
        )
        late[s] = [frame_name(r) for r in late_refs[:20]]
        late_spans[s] = [
            {
                "frame": frame_name(r),
                "start_ns": entries[r].min_event_ns,
                "end_ns": entries[r].max_event_ns + math.ceil((mine[r]["sent_ns"] - mine[r]["due_ns"]) / NS) * NS,
                "late_s": round((mine[r]["sent_ns"] - mine[r]["due_ns"]) / NS, 3),
            }
            for r in late_refs
            if r in entries
        ]
        n_rej = sum(1 for o in mine.values() if o["status"] == "rejected")
        missing = expected[s][0] - len(mine)
        checked = [o["time_left"] for o in ok if o.get("time_left") is not None]
        left = sum(checked) if checked else None
        signals[s] = SignalReport(
            frames_expected=expected[s][0],
            frames_ok=len(ok),
            frames_rejected=n_rej,
            frames_missing=missing,
            records_expected=expected[s][1],
            records_ok=sum(o["records"] for o in ok),
            late_bound_s=bound,
            late_frames=len(late_refs),
            late_p50_s=_quantile(lates, 0.5),
            late_p99_s=_quantile(lates, 0.99),
            late_max_s=round(max(lates), 3) if lates else None,
            retries=retries[s],
            refused_records=refused[s],
            rejected_records=sum(o["records"] for o in mine.values() if o["status"] == "rejected"),
            time_strings_left=left,
            complete=missing == 0 and n_rej == 0 and not late_refs and len(ok) == expected[s][0] and not left,
        )
    for r in sorted(r for r, o in outcomes.items() if o["status"] == "rejected"):
        o = outcomes[r]
        rejected.append({"frame": frame_name(r), "http": o["http"], "body": o["body"]})
    finished = all(v.frames_missing == 0 for v in signals.values())
    return StackReport(
        stack=stack,
        session=session.id,
        finished=finished,
        complete=all(v.complete for v in signals.values()),
        signals=signals,
        rejected=rejected,
        in_doubt=[frame_name(r) for r in sorted(doubt)],
        late=late,
        late_spans=late_spans,
        last_outcome_ns=max((o["done_ns"] for o in outcomes.values()), default=None),
        arrival_horizon_s=horizon_of(entries[r] for r in outcomes if r in entries) if outcomes else None,
        frame_spans=frame_spans,
        query_horizon_s=math.ceil(max(reaches) / NS) if reaches else None,
    )
