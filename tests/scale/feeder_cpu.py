"""Replay feeder CPU: the per-frame work of `otb ingest`, without sending.

  uv run python tests/scale/feeder_cpu.py CORPUS [--processes 4] [--threads 4] [--out FILE]

For every frame of a sealed corpus, in due order: read and decompress it, shift it (`otb.ingest.replay_body`) and
count the recording-time strings left (`otb.timeshift.recording_time_left`), as a replay feeder does. Passes:
  replay one_process   one pass in this process: one feeder's rate, and its rate per CPU second
  replay threads       --threads passes as threads of one process: what the GIL leaves (the pre-U10 shape)
  replay processes     --processes passes as separate processes: `otb ingest`'s shape, one process per stack
  follow one_process   one pass at shift 0, as follow mode during a capture: read and decompress only
HTTP sends are not included (tests/scale/ingest_throughput.py includes them). The corpus is only read.

The shift is minus (recording span + 4 days), so every band clears the recording days and the leftover scan runs,
as it does in a real replay. Writes CORPUS/derived/scale/feeder_cpu.json unless --out is given; `otb
scale-report` reads it from there.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import threading
import time
from pathlib import Path

from otb.archive import Archive
from otb.host import describe
from otb.ingest import replay_body
from otb.timebase import NS, Shift
from otb.timeshift import Band, recording_days, recording_time_left, shift_clears

DAY_S = 86_400


def clearing_shift(root: Path) -> int:
    span = Archive(root).manifest.window
    return -((span.end - span.start) // NS + 4 * DAY_S) // 60 * 60


def one_pass(root: Path, shift_s: int) -> tuple[int, float, float]:
    """(raw bytes, wall seconds, CPU seconds of this thread's process) for one feeder's preparation."""
    archive = Archive(root)
    span = archive.manifest.window
    clears = shift_clears(shift_s, span.start, span.end)
    days = recording_days(span.start, span.end)
    shift = Shift(shift_s)
    raw = 0
    t0, c0 = time.perf_counter(), time.process_time()
    for ref, e in archive.merged():
        body = replay_body(archive.read(ref, e), e, shift)
        if clears:
            recording_time_left(body, days, Band.of(e.min_event_ns, e.max_event_ns))
        raw += len(body)
    return raw, time.perf_counter() - t0, time.process_time() - c0


def rate(raw: int, wall: float, cpu: float | None = None) -> dict:
    out = {"s": round(wall, 2), "raw_mb_per_s": round(raw / wall / 1e6, 1)}
    if cpu:
        out["raw_mb_per_cpu_s"] = round(raw / cpu / 1e6, 1)
    return out


def threads(root: Path, shift_s: int, n: int) -> dict:
    results: list[tuple[int, float, float]] = []
    ts = [threading.Thread(target=lambda: results.append(one_pass(root, shift_s))) for _ in range(n)]
    t0 = time.perf_counter()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return {"n": n} | rate(sum(r for r, _, _ in results), time.perf_counter() - t0)


def processes(root: Path, shift_s: int, n: int) -> dict:
    with multiprocessing.get_context("spawn").Pool(n) as pool:
        t0 = time.perf_counter()
        results = pool.starmap(one_pass, [(root, shift_s)] * n)
        wall = time.perf_counter() - t0
    return {"n": n, "slowest_process_s": round(max(w for _, w, _ in results), 2)} | rate(
        sum(r for r, _, _ in results), wall
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("corpus", type=Path)
    p.add_argument("--processes", type=int, default=4, help="feeder processes, one per stack")
    p.add_argument("--threads", type=int, default=4)
    p.add_argument("--out", type=Path, default=None)
    a = p.parse_args()
    out = a.out or a.corpus / "derived" / "scale" / "feeder_cpu.json"
    shift_s = clearing_shift(a.corpus)
    raw, wall, cpu = one_pass(a.corpus, shift_s)
    replay = {"one_process": rate(raw, wall, cpu)}
    if a.threads:
        replay["threads"] = threads(a.corpus, shift_s, a.threads)
    if a.processes:
        replay["processes"] = processes(a.corpus, shift_s, a.processes)
    raw0, wall0, cpu0 = one_pass(a.corpus, 0)
    doc = {
        "benchmark": "feeder_cpu",
        "corpus": a.corpus.name,
        "corpus_id": Archive(a.corpus).manifest.corpus_id,
        "host": describe(),
        "shift_s": shift_s,
        "raw_bytes": raw,
        "replay": replay,
        "follow": {"one_process": rate(raw0, wall0, cpu0)},
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=1) + "\n")
    print(json.dumps(doc, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
