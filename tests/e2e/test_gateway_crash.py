"""Gateway crash safety: SIGKILL the gateway three times while four concurrent clients post the corpus-mini frames.

The clients behave like the demo collector's exporter: a batch without a 200 is resent until it gets one. After
each kill, before the restart, the open segments are inspected for a torn tail. After each restart, batches the
gateway already acked are resent too, as a collector does when an ack is lost in the crash: the newest acked
batch of each signal, and one acked batch from a segment sealed before the crash.

Ways it could fail, each asserted below:
- an acked batch is missing after restart (fsync before 200 broken, or recovery truncates too much);
- a resent batch is stored twice (dedupe window not refilled on restart, or not reaching sealed segments);
- a torn frame or torn index line survives into the sealed archive, or full verify finds a problem;
- the record totals differ from the fixture's in any signal;
- the restarted gateway refuses the archive.

Evidence: tests/e2e/artifacts/gateway_crash.json.
"""

import asyncio
import hashlib
from pathlib import Path

import aiohttp
import orjson
from conftest import free_port, ok, otb, write_artifact
from gateway_proc import MINI, GatewayProc, health, mini_frames, post

from otb.archive import Archive

REPO = Path(__file__).resolve().parents[2]
KILL_AT_ACKS = (40, 110, 180)
CLIENTS = 4
HEADERS = {"Content-Type": "application/json"}


def open_tails(out: Path) -> dict[str, int]:
    """Bytes past the last indexed frame, and index files whose last line is torn, across the open segments."""
    torn_bytes = torn_lines = 0
    for p in out.glob("segments/*/*/*.otlp.zst.open"):
        data = p.with_name(p.name[:6] + ".idx.jsonl").read_bytes()
        complete = data[: data.rfind(b"\n") + 1]
        torn_lines += len(complete) != len(data)
        entries = [orjson.loads(line) for line in complete.splitlines()]
        end = entries[-1]["offset"] + entries[-1]["length"] if entries else 0
        torn_bytes += p.stat().st_size - end
    return {"bytes_past_last_index": torn_bytes, "torn_index_lines": torn_lines}


def sealed_shas(out: Path) -> set[str]:
    a = Archive(out)
    return {e.body_sha256 for seg in a.segments() for e in a.index(seg.signal, seg.stream, seg.seq)}


def test_gateway_crash(tmp_path: Path, artifacts: Path) -> None:
    frames = mini_frames()
    sha = [hashlib.sha256(b).hexdigest() for _, b in frames]
    out = tmp_path / "corpus"
    gw = GatewayProc(out, free_port(), tmp_path / "gateway.log", "--rotate-mb", "0.25")
    gw.start()
    acked: list[int] = []
    attempts = [0] * len(frames)
    crashes: list[dict] = []
    killed_run_dedupe_hits: list[int] = []
    kills = list(KILL_AT_ACKS)
    up = asyncio.Event()
    up.set()

    def crash_and_restart() -> None:
        killed_run_dedupe_hits.append(health(gw.url)["totals"]["dedupe_hits"])
        gw.kill()
        tails = open_tails(out)
        gw.start()
        in_sealed = sealed_shas(out)
        newest = {s: next(i for i in reversed(acked) if frames[i][0] == s) for s in {frames[i][0] for i in acked}}
        old = next(i for i in acked if sha[i] in in_sealed)
        replays = sorted(set(newest.values()) | {old})
        statuses = [post(gw.url, *frames[i])[0] for i in replays]
        crashes.append(
            {
                "after_acks": len(acked),
                "open_tails": tails,
                "replayed_acked": len(replays),
                "replay_statuses": statuses,
                "replayed_from_sealed_segment": sha[old] in in_sealed,
            }
        )

    async def worker(session: aiohttp.ClientSession, queue: asyncio.Queue) -> None:
        while not queue.empty():
            i = queue.get_nowait()
            signal, body = frames[i]
            while True:
                await up.wait()
                attempts[i] += 1
                try:
                    async with session.post(f"{gw.url}/v1/{signal}", data=body, headers=HEADERS) as r:
                        await r.read()
                        if r.status == 200:
                            break
                except aiohttp.ClientError:
                    pass
                await asyncio.sleep(0.05)
            acked.append(i)
            if kills and len(acked) >= kills[0]:
                kills.pop(0)
                up.clear()
                await asyncio.to_thread(crash_and_restart)
                up.set()

    async def drive() -> None:
        queue: asyncio.Queue = asyncio.Queue()
        for i in range(len(frames)):
            queue.put_nowait(i)
        async with aiohttp.ClientSession() as session:
            await asyncio.gather(*(worker(session, queue) for _ in range(CLIENTS)))

    asyncio.run(drive())
    exit_code = gw.term()

    verify = ok(otb("corpus", "verify", out, "--level", "full"))
    a = Archive.open(out, verify="full")
    m = a.manifest
    src = Archive.open(MINI, verify="index").manifest
    signals = ("traces", "logs", "metrics")
    stored = sorted(e.body_sha256 for _, e in a.merged())
    reports = gw.reports()
    timing = {
        "resent_unacked": sum(attempts) - len(frames),
        "crashes": crashes,
        "dedupe_hits_per_run": [*killed_run_dedupe_hits, reports[-1]["totals"]["dedupe_hits"]],
    }
    result = {
        "frames_posted": len(frames),
        "acked": len(acked),
        "crashes": len(crashes),
        "every_replay_answered_200": all(set(c["replay_statuses"]) == {200} for c in crashes),
        "every_crash_replayed_from_a_sealed_segment": all(c["replayed_from_sealed_segment"] for c in crashes),
        "gateway_exit": exit_code,
        "verify": verify.stdout.strip().splitlines()[-1].split(": ", 1)[1],
        "frames_stored": len(stored),
        "distinct_bodies_stored": len(set(stored)),
        "stored_equals_posted": stored == sorted(sha),
        "records": {s: m.totals[s].records for s in signals},
        "fixture_records": {s: src.totals[s].records for s in signals},
    }
    write_artifact(artifacts / "gateway_crash.json", result, timing=timing)

    replays = sum(c["replayed_acked"] for c in crashes)
    after = [c["after_acks"] for c in crashes]
    assert len(after) == len(KILL_AT_ACKS) and all(a >= k for a, k in zip(after, KILL_AT_ACKS, strict=True))
    assert sum(timing["dedupe_hits_per_run"]) >= replays > 0
    assert result == {
        "frames_posted": 232,
        "acked": 232,
        "crashes": len(KILL_AT_ACKS),
        "every_replay_answered_200": True,
        "every_crash_replayed_from_a_sealed_segment": True,
        "gateway_exit": 0,
        "verify": "full verify ok (0 problems)",
        "frames_stored": 232,
        "distinct_bodies_stored": 232,
        "stored_equals_posted": True,
        "records": {"traces": 24323, "logs": 8394, "metrics": 40712},
        "fixture_records": {"traces": 24323, "logs": 8394, "metrics": 40712},
    }
