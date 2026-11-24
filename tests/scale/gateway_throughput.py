"""Gateway throughput: drive one gateway subprocess with a whole recording as fast as it acks.

  uv run python tests/scale/gateway_throughput.py (--recording DIR | --corpus DIR) [--modes per-signal,8-clients,8-gzip]
      [--scratch DIR] [--out FILE]

The input is a v2 recording (`<signal>.jsonl`, one export request per line) or an otb corpus archive (every frame in
due order, as stored). Each mode starts a fresh gateway (`otb capture --gateway-only`) on a scratch corpus and posts
every request once:
  per-signal   3 clients, one per signal, each posting its file in order (the collector's shape)
  N-clients    N clients sharing one queue of all requests
  N-gzip       as N-clients, bodies gzip-compressed before timing (the demo overlay's setting)
Reported per mode: raw MB/s and batches/s over first send to last ack, ack latency quantiles, the gateway's peak
RSS and CPU from its own report, dedupe hits, and the sealed archive's frame and record totals.

--scratch puts the gateway's archive on the disk under test: the gateway fsyncs every frame, and a laptop's fsync
is cheaper than a Linux host's. With --corpus the default --out is CORPUS/derived/scale/gateway_throughput.json,
where `otb scale-report` reads it.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import socket
import sys
import tempfile
import time
from collections import deque
from pathlib import Path

import aiohttp

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests/e2e"))

from gateway_proc import GatewayProc, health  # noqa: E402

from otb.archive import Archive  # noqa: E402
from otb.host import describe  # noqa: E402

SIGNALS = ("traces", "logs", "metrics")
TB_BYTES = 1e12
TB_WINDOW_S = 72 * 3600
HEADROOM = 3


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def load(recording: Path) -> dict[str, list[bytes]]:
    out = {}
    for s in SIGNALS:
        with (recording / f"{s}.jsonl").open("rb") as f:
            out[s] = [line.rstrip(b"\n") for line in f if line.strip()]
    return out


def load_corpus(corpus: Path) -> dict[str, list[bytes]]:
    archive = Archive(corpus)
    out: dict[str, list[bytes]] = {s: [] for s in SIGNALS}
    for ref, e in archive.merged():
        if e.records:
            out[ref.signal].append(archive.read(ref, e))
    return out


def parse_mode(name: str) -> tuple[int, bool]:
    """(clients, gzip): per-signal is 3 plain clients, one per signal; N-clients and N-gzip share one queue."""
    if name == "per-signal":
        return 3, False
    n, _, kind = name.partition("-")
    if not n.isdigit() or kind not in ("clients", "gzip"):
        raise SystemExit(f"unknown mode {name!r}: per-signal, N-clients or N-gzip")
    return int(n), kind == "gzip"


def quantile(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


async def drive(url: str, queues: list[deque], per_queue: int, gzipped: bool) -> tuple[float, list[float], int]:
    """per_queue clients drain each queue; returns wall seconds, per-request ack latencies, 503 retries."""
    latencies: list[float] = []
    retries = 0
    headers = {"Content-Type": "application/json"} | ({"Content-Encoding": "gzip"} if gzipped else {})

    async def client(session: aiohttp.ClientSession, q: deque) -> None:
        nonlocal retries
        while q:
            signal, body = q.popleft()
            while True:
                t0 = time.perf_counter()
                async with session.post(f"{url}/v1/{signal}", data=body, headers=headers) as r:
                    await r.read()
                if r.status == 200:
                    latencies.append(time.perf_counter() - t0)
                    break
                if r.status != 503:
                    raise RuntimeError(f"{signal}: HTTP {r.status}")
                retries += 1
                await asyncio.sleep(0.01)

    conn = aiohttp.TCPConnector(limit=0)
    async with aiohttp.ClientSession(connector=conn, timeout=aiohttp.ClientTimeout(total=None)) as session:
        t0 = time.perf_counter()
        await asyncio.gather(*(client(session, q) for q in queues for _ in range(per_queue)))
        wall = time.perf_counter() - t0
    return wall, latencies, retries


def run_mode(name: str, lines: dict[str, list[bytes]], clients: int, gzipped: bool, scratch: Path) -> dict:
    raw_bytes = sum(len(b) for v in lines.values() for b in v)
    batches = sum(len(v) for v in lines.values())

    def enc(b: bytes) -> bytes:
        return gzip.compress(b, 1) if gzipped else b

    if name == "per-signal":
        queues, per_queue = [deque((s, enc(b)) for b in lines[s]) for s in SIGNALS], 1
    else:
        rounds = max(len(v) for v in lines.values())
        queues = [deque((s, enc(lines[s][i])) for i in range(rounds) for s in SIGNALS if i < len(lines[s]))]
        per_queue = clients
    wire_bytes = sum(len(b) for q in queues for _, b in q)
    out = scratch / name
    gw = GatewayProc(out, free_port(), scratch / f"{name}.log")
    gw.start()
    wall, lat, retries = asyncio.run(drive(gw.url, queues, per_queue, gzipped))
    live = health(gw.url)
    assert gw.term() == 0
    report = gw.reports()[-1]
    m = Archive.open(out, verify="index").manifest
    cpu_s = report["cpu_user_s"] + report["cpu_sys_s"]
    return {
        "clients": clients,
        "gzip": gzipped,
        "batches": batches,
        "raw_mb": round(raw_bytes / 1e6, 1),
        "wire_mb": round(wire_bytes / 1e6, 1),
        "wall_s": round(wall, 2),
        "raw_mb_per_s": round(raw_bytes / 1e6 / wall, 1),
        "batches_per_s": round(batches / wall, 1),
        "ack_latency_ms": {q: round(quantile(lat, v) * 1e3, 1) for q, v in (("p50", 0.5), ("p99", 0.99), ("max", 1))},
        "busy_503_retries": retries,
        "gateway": {
            "peak_rss_mb": report["peak_rss_mb"],
            "cpu_s": round(cpu_s, 2),
            "cpu_cores_busy": round(cpu_s / wall, 2),
            "dedupe_hits": report["totals"]["dedupe_hits"],
            "rejects": report["rejects"],
            "pending_at_end": live["pending"] if live else None,
        },
        "archive": {
            "frames": {s: m.totals[s].frames for s in SIGNALS},
            "records": {s: m.totals[s].records for s in SIGNALS},
            "compressed_mb": round(sum(m.totals[s].bytes for s in SIGNALS) / 1e6, 1),
        },
    }


def main() -> int:
    p = argparse.ArgumentParser()
    src = p.add_mutually_exclusive_group()
    src.add_argument("--recording", type=Path, default=REPO / "corpus/dryrun/recording")
    src.add_argument("--corpus", type=Path, default=None)
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--modes", default="per-signal,8-clients,8-gzip")
    p.add_argument("--scratch", type=Path, default=None, help="where the gateway writes (default: a temp dir)")
    a = p.parse_args()
    lines = load_corpus(a.corpus) if a.corpus else load(a.recording)
    out = a.out or REPO / "docs/v1/units/U9a-throughput.json"
    if a.corpus and not a.out:
        out = a.corpus / "derived" / "scale" / "gateway_throughput.json"
    results = {}
    with tempfile.TemporaryDirectory(prefix="otb-gw-", dir=a.scratch) as scratch:
        for name in a.modes.split(","):
            results[name] = run_mode(name, lines, *parse_mode(name), Path(scratch))
            print(name, json.dumps(results[name]), flush=True)
    need = TB_BYTES / TB_WINDOW_S / 1e6
    worst = min(r["raw_mb_per_s"] for r in results.values())
    doc = {
        "benchmark": "gateway_throughput",
        "input": str(a.corpus) if a.corpus else {s: f"{s}.jsonl" for s in SIGNALS},
        "host": describe(),
        "modes": results,
        "requirement": {
            "tb_in_72h_mb_per_s": round(need, 2),
            "with_3x_headroom_mb_per_s": round(need * HEADROOM, 2),
            "slowest_mode_mb_per_s": worst,
            "headroom_over_1tb_rate": round(worst / need, 1),
            "gateways_needed_at_3x": max(1, int(-(-need * HEADROOM // worst))),
        },
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=1) + "\n")
    print(json.dumps(doc["requirement"], indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
