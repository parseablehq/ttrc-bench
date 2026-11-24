"""The gate plan over corpus-mini is the committed golden (tests/fixtures/gate-plan-mini.json, made by
tests/fixtures/make_gate_plan.py), the long traces it fetches by id are those the old span-by-span walk found,
cut to the windows the plan searches, and that walk now stays inside DuckDB: a 5 M-span synthetic store is
aggregated in a subprocess whose peak RSS stays bounded (tests/e2e/long_traces_bench.py).

Ways it could be wrong, written before the code:
 1. A point or window of the plan moves: the sampled part of a plan must never move (write_plan refuses it).
 2. long_traces finds other traces than the walk did: a frame's due time read from the wrong frame, the spread
    taken over one window's spans instead of every span of the trace, the > bound read as >=, start times
    unsorted or missing.
 3. A long trace with a span starting in a searched window is left out, so B1 counts its search fragment one
    short; or one with no span in any searched window is fetched, which grows plan.json for nothing.
 4. The by-id decision per point differs from the old per-point predicate.
 5. Memory grows with the store because the (trace, seg, frame) rows still come into Python, or time grows with
    spans x windows because the candidate scan runs as a range join over the hundreds of windows a suite adds.
 6. build_plan no longer attaches the fetch list, or attaches it under another live window than the stack's.

Evidence: tests/e2e/artifacts/gate_plan.json.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from conftest import write_artifact
from fixtures.make_gate_plan import LIVE_S, OUT, STACK, plan_mini

from otb.archive import Archive
from otb.gate import Plan, build_plan, by_id_points, frame_dues, is_trace_search, long_traces, trace_windows
from otb.ingest import Session
from otb.ref.build import build
from otb.ref.store import ReferenceStore
from otb.timebase import NS

REPO = Path(__file__).resolve().parents[2]
MINI = REPO / "tests/fixtures/corpus-mini"
BENCH_TRACES = 1_000_000
BENCH_WINDOWS = 500  # a plan with a suite searches hundreds of windows; a range join over them costs spans x windows
PEAK_RSS_MB = 1000  # DuckDB's 1 GB limit plus the interpreter; the old walk takes several times that


def walk(store: ReferenceStore, archive: Archive, live_window_s: float) -> dict[str, list[int]]:
    """The gate's walk before long_traces moved into DuckDB: every (trace, seg, frame) row into Python."""
    frames = store.con.execute("SELECT DISTINCT trace_id, seg, frame FROM spans ORDER BY ALL").fetchall()
    index: dict[str, list] = {}
    due: dict[str, list[int]] = {}
    for t, seg, fr in frames:
        if seg not in index:
            signal, stream, seq = seg.split("-")
            index[seg] = archive.index(signal, stream, int(seq))  # type: ignore[arg-type]
        due.setdefault(t, []).append(index[seg][fr].max_event_ns)
    long = sorted(t for t, d in due.items() if max(d) - min(d) > live_window_s * NS)
    rows = store.con.execute(
        "SELECT trace_id, list(start_ns ORDER BY start_ns) FROM spans WHERE trace_id IN (SELECT unnest(?::VARCHAR[])) "
        "GROUP BY trace_id",
        [long],
    ).fetchall()
    return dict(sorted(rows))


def test_plan_is_the_golden_and_long_traces_match_the_walk(tmp_path: Path, artifacts: Path) -> None:
    corpus = tmp_path / "corpus-mini"
    shutil.copytree(MINI, corpus)
    build(corpus, workers=2)
    plan = plan_mini(corpus)
    golden = Plan.load(OUT)
    assert plan.to_json() == OUT.read_bytes()
    assert [p.id for p in plan.points] == [p.id for p in golden.points] and plan.windows == golden.windows

    archive, store = Archive(corpus), ReferenceStore.open(corpus)
    scope = archive.manifest.window
    everywhere = {}
    for live in (60.0, 10.0, 1.0):
        old = walk(store, archive, live)
        everywhere[live] = len(old)
        assert long_traces(store, frame_dues(archive), live, [scope]) == old, f"live window {live} s"
    assert everywhere == {60.0: 0, 10.0: 251, 1.0: 2846}

    searched = trace_windows(plan)
    old = walk(store, archive, LIVE_S)
    cut = {t: s for t, s in old.items() if any(w.start <= x < w.end for w in searched for x in s)}
    assert plan.by_id_traces(STACK) == cut
    assert 0 < len(cut) < len(old), "the plan searches some minutes of corpus-mini, not all of it"
    assert long_traces(store, frame_dues(archive), LIVE_S, []) == {}

    needs = {
        p.id
        for p in plan.points
        if is_trace_search(p) and any(p.window.start <= t < p.window.end for s in cut.values() for t in s)  # type: ignore[union-attr]
    }
    assert by_id_points(plan, STACK) == frozenset(needs) and needs
    assert by_id_points(plan, "b3-clickhouse") == frozenset()

    session = Session(
        id="plan-mini",
        corpus=str(corpus),
        corpus_id=archive.manifest.corpus_id,
        mode="replay",
        shift_s=0,
        lead_s=0,
        window=None,
        stacks=(STACK, "b3-clickhouse"),
        created_utc="2026-10-12T00:00:00Z",
        arrival_horizon_s=None,
    )
    wired, reference = build_plan(session, 20261002, 3, 4, None, REPO)
    assert [p.id for p in wired.points] == [p.id for p in plan.points] and len(reference) == len(plan.points)
    assert wired.fetch_by_id == {STACK: {"live_window_s": 60.0, "traces": {}}}, (
        "build_plan attaches the fetch list under B1's real live window; no trace of corpus-mini spreads over 60 s"
    )

    bench = subprocess.run(
        [
            sys.executable,
            str(REPO / "tests/e2e/long_traces_bench.py"),
            str(tmp_path / "bench"),
            str(BENCH_TRACES),
            "--windows",
            str(BENCH_WINDOWS),
        ],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO,
    )
    numbers = json.loads(bench.stdout.strip().splitlines()[-1])
    write_artifact(
        artifacts / "gate_plan.json",
        {
            "points": len(plan.points),
            "windows": sorted(plan.windows),
            "searched_windows": [[w.start, w.end] for w in searched],
            "long_traces_everywhere": everywhere,
            "fetched_by_id": {"everywhere": len(old), "in_searched_windows": len(cut)},
            "points_counted_with_by_id": len(needs),
            "bench": {k: v for k, v in numbers.items() if k not in ("seconds", "peak_rss_mb")},
        },
        timing={k: numbers[k] for k in ("seconds", "peak_rss_mb")},
    )
    assert numbers["n_spans"] == 5 * BENCH_TRACES and numbers["found"] == BENCH_TRACES // 100
    assert numbers["windows"] == BENCH_WINDOWS
    assert numbers["correct"]
    assert numbers["peak_rss_mb"] < PEAK_RSS_MB, numbers
