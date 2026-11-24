"""An index-only archive and an ingest ledger of any size, for the ledger memory test (tests/e2e/test_ledger_scale.py).

The archive has segments.jsonl and one index file per segment and no frame bodies: `stack_report` and `Ledger` read
indexes only. The ledger has every event the feeder writes, in the proportions a long follow session shows, plus
the shapes that make a naive fold wrong: a frame in doubt across a feeder restart, a transport error in doubt
followed by an outcome, a rejection, retries with and without a status, frames past the lateness bound, a second
outcome line for one frame, and a torn last line.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import orjson

from otb.timebase import NS

SIGNALS = ("traces", "logs", "metrics")
T0_NS = 1790237400 * NS


def write_index_only_archive(root: Path, frames: int, *, per_segment: int = 500, stream: str = "g0") -> None:
    """`frames` frames per signal, `per_segment` to a segment, one record more than the previous frame, due
    every 100 ms. Every frame has a 10 s event span, except the first of each segment (a span of 0)."""
    (root / "segments").mkdir(parents=True, exist_ok=True)
    with (root / "segments.jsonl").open("wb") as segs:
        for signal in SIGNALS:
            d = root / "segments" / signal / stream
            d.mkdir(parents=True, exist_ok=True)
            for seq in range(0, frames, per_segment):
                n = min(per_segment, frames - seq)
                lines = []
                for i in range(n):
                    k = seq + i
                    end = T0_NS + k * NS // 10
                    lines.append(
                        {
                            "index": i,
                            "offset": i * 1000,
                            "length": 1000,
                            "records": 1 + k % 40,
                            "min_event_ns": end if i == 0 else end - 10 * NS,
                            "max_event_ns": end,
                            "body_sha256": hashlib.sha256(f"{signal}{k}".encode()).hexdigest(),
                            "received_ns": end + NS,
                            "dropped": {},
                        }
                    )
                index = b"".join(orjson.dumps(x) + b"\n" for x in lines)
                number = seq // per_segment
                (d / f"{number:06d}.idx.jsonl").write_bytes(index)
                segs.write(
                    orjson.dumps(
                        {
                            "signal": signal,
                            "stream": stream,
                            "seq": number,
                            "path": f"segments/{signal}/{stream}/{number:06d}.otlp.zst",
                            "bytes": n * 1000,
                            "sha256": "0" * 64,
                            "index_sha256": hashlib.sha256(index).hexdigest(),
                            "frames": n,
                            "records": sum(x["records"] for x in lines),
                            "min_event_ns": lines[0]["min_event_ns"],
                            "max_event_ns": lines[-1]["max_event_ns"],
                        }
                    )
                    + b"\n"
                )


def write_ledger(path: Path, frames: int, *, per_segment: int = 500, stream: str = "g0", torn: bool = True) -> dict:
    """The ledger of a feeder that sent every frame of `write_index_only_archive(frames)`, with the shapes listed in
    the module docstring at fixed positions. Returns the frame names it treated specially, by shape."""
    special: dict[str, list[list]] = {k: [] for k in ("rejected", "in_doubt", "late", "duplicate")}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:

        def put(obj: dict) -> None:
            f.write(orjson.dumps(obj) + b"\n")

        put({"ev": "start", "at_ns": T0_NS, "in_doubt": 0})
        for signal in SIGNALS:
            for k in range(frames):
                ref = [signal, stream, k // per_segment, k % per_segment]
                due = T0_NS + k * NS // 10
                records = 1 + k % 40
                put({"ev": "intent", "f": ref, "due_ns": due, "at_ns": due + 1_000_000})
                late_ns = 500_000_000 + (k * 7919) % 3_000_000_000  # 0.5 s to 3.5 s
                if k % 1009 == 7:
                    late_ns = 20 * NS + k % 5 * NS  # past a 10 s bound
                    special["late"].append(ref)
                sent = due + late_ns
                if k % 503 == 11:
                    put(
                        {
                            "ev": "retry",
                            "f": ref,
                            "attempt": 1,
                            "http": 503,
                            "error": "",
                            "in_doubt": False,
                            "records": records,
                            "at_ns": sent,
                        }
                    )
                if k % 701 == 13:
                    put(
                        {
                            "ev": "retry",
                            "f": ref,
                            "attempt": 1,
                            "http": None,
                            "error": "TimeoutError: timed out",
                            "in_doubt": True,
                            "records": records,
                            "at_ns": sent,
                        }
                    )
                    special["in_doubt"].append(ref)
                if k % 907 == 17:
                    put(
                        {
                            "ev": "retry",
                            "f": ref,
                            "attempt": 1,
                            "http": 429,
                            "error": "",
                            "in_doubt": False,
                            "records": records,
                            "at_ns": sent,
                        }
                    )
                if signal == "logs" and k % 1511 == 19:
                    put({"ev": "start", "at_ns": sent + NS, "in_doubt": 1})  # the feeder died with this intent open
                    special["in_doubt"].append(ref)
                    put({"ev": "intent", "f": ref, "due_ns": due, "at_ns": sent + 2 * NS})
                    sent += 2 * NS
                rejected = k % 1201 == 23
                if rejected:
                    special["rejected"].append(ref)
                outcome = {
                    "ev": "outcome",
                    "f": ref,
                    "status": "rejected" if rejected else "ok",
                    "http": 400 if rejected else 200,
                    "attempts": 1,
                    "due_ns": due,
                    "sent_ns": sent,
                    "done_ns": sent + 50_000_000,
                    "records": records,
                    "time_left": (k % 3 if k % 97 == 0 else None) if signal == "logs" else None,
                    "body": '{"error":"synthetic rejection"}' if rejected else None,
                }
                put(outcome)
                if k % 1301 == 29 and not rejected:
                    special["duplicate"].append(ref)
                    put(outcome | {"sent_ns": sent + NS, "done_ns": sent + NS + 50_000_000, "attempts": 2})
        if torn:
            f.write(b'{"ev":"outcome","f":["traces"')
    return special
