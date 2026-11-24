"""Peak memory of otb.gate.long_traces on a synthetic spans store of N_TRACES x 5 spans, in its own process so the
peak RSS is this walk's alone. Prints one JSON line: the span count, how many long traces were found, whether they
are exactly the ones the construction predicts, the wall time and the peak RSS in MB.

  uv run python tests/e2e/long_traces_bench.py OUT_DIR N_TRACES [--old] [--windows N]

Every trace i has five spans starting at i, i+1, ... i+4 seconds, each in its own frame sent one second after its
start, except that the last span of every tenth trace is sent 120 s after: those traces arrive spread over more
than B1's 60 s live window. The searched range is the first tenth of the timeline, tiled into --windows adjacent
windows (a plan searches hundreds), so the expected answer is every tenth trace of the first tenth, with all five
start times. --old runs the walk the gate used before, which pulled every (trace, seg, frame) row into Python,
for comparison.
"""

from __future__ import annotations

import json
import platform
import resource
import sys
import time
from collections.abc import Iterable, Iterator
from itertools import pairwise
from pathlib import Path

import duckdb

from otb.gate import long_traces
from otb.ref.store import ReferenceStore
from otb.timebase import NS, EventNs, Window

SPANS_PER_TRACE = 5
LIVE_S = 60.0
SEG = "traces-g0-000000"


def dues(n: int) -> Iterator[tuple[str, int, int]]:
    for i in range(n):
        for j in range(SPANS_PER_TRACE):
            late = 120 if (i % 10 == 0 and j == SPANS_PER_TRACE - 1) else 1
            yield SEG, i * SPANS_PER_TRACE + j, (i + j + late) * NS


def old_long_traces(
    con: duckdb.DuckDBPyConnection, frame_dues: Iterable[tuple[str, int, int]], live_window_s: float
) -> dict:
    index = [d for _, _, d in frame_dues]
    frames = con.execute("SELECT DISTINCT trace_id, seg, frame FROM spans ORDER BY ALL").fetchall()
    due: dict[str, list[int]] = {}
    for t, _, fr in frames:
        due.setdefault(t, []).append(index[fr])
    long = sorted(t for t, d in due.items() if max(d) - min(d) > live_window_s * NS)
    rows = con.execute(
        "SELECT trace_id, list(start_ns ORDER BY start_ns) FROM spans WHERE trace_id IN (SELECT unnest(?::VARCHAR[])) "
        "GROUP BY trace_id",
        [long],
    ).fetchall()
    return dict(sorted(rows))


def main() -> None:
    out, n, old = Path(sys.argv[1]), int(sys.argv[2]), "--old" in sys.argv
    out.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET memory_limit = '1GB'")
    con.execute("SET threads = 2")
    con.execute(f"SET temp_directory = '{out / 'duckdb_tmp'}'")
    parquet = out / "spans.parquet"
    con.execute(
        f"""
        COPY (
            SELECT lpad(lower(to_hex(i)), 32, '0') AS trace_id, '{SEG}' AS seg,
                   CAST(i * {SPANS_PER_TRACE} + j AS INTEGER) AS frame, (i + j) * {NS} AS start_ns
            FROM range({n}) t(i), range({SPANS_PER_TRACE}) s(j)
        ) TO '{parquet}' (FORMAT PARQUET)
        """
    )
    con.execute(f"CREATE VIEW spans AS SELECT * FROM read_parquet('{parquet}')")
    pieces = int(sys.argv[sys.argv.index("--windows") + 1]) if "--windows" in sys.argv else 1
    edges = [k * (n // 10) // pieces for k in range(pieces + 1)]  # the first tenth of the timeline, tiled
    windows = [Window(EventNs(a * NS), EventNs(b * NS)) for a, b in pairwise(edges)]
    t0 = time.monotonic()
    if old:
        found = old_long_traces(con, dues(n), LIVE_S)
        found = {t: s for t, s in found.items() if any(0 <= x < (n // 10) * NS for x in s)}
    else:
        found = long_traces(ReferenceStore(con), dues(n), LIVE_S, windows)
    seconds = time.monotonic() - t0
    expected = {f"{i:032x}": [(i + j) * NS for j in range(SPANS_PER_TRACE)] for i in range(0, n // 10, 10)}
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(
        json.dumps(
            {
                "mode": "old" if old else "duckdb",
                "n_spans": n * SPANS_PER_TRACE,
                "windows": pieces,
                "found": len(found),
                "correct": found == expected,
                "seconds": round(seconds, 1),
                "peak_rss_mb": round(rss / (1 << 20) if platform.system() == "Darwin" else rss / 1024),
            }
        )
    )


if __name__ == "__main__":
    main()
