"""Unpack one demo instance's frames of a sealed corpus archive into the raw OTLP/JSON recording that
tests/independent/task_truth.py reads: <out>/{traces,logs,metrics}.jsonl, one export request per line.

Reads the archive's own files (segments.jsonl, each segment's .idx.jsonl and zstd frames) without importing otb, and
checks every frame's body sha256 and each segment's frame and record counts. A frame is kept whole, byte for byte,
when its body holds the JSON string of the region: a resource whose cloud.region is that region cannot be serialised
without it, so every frame dropped is one the checker would skip in full. A sample of the dropped frames is parsed to
confirm it. The checker applies its own region filter to what is kept.

    python k8s/gke/r2_unpack.py <corpus> <out dir> <region> [workers]

Writes <out>/unpack.json with the totals read and kept per signal.
"""

from __future__ import annotations

import hashlib
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import zstandard

SIGNALS = ("traces", "logs", "metrics")
RESOURCES = {"traces": "resourceSpans", "logs": "resourceLogs", "metrics": "resourceMetrics"}
SAMPLE = 200


def regions(signal: str, body: bytes) -> set:
    found = set()
    for r in json.loads(body).get(RESOURCES[signal], []):
        attrs = {a["key"]: a["value"] for a in r.get("resource", {}).get("attributes", [])}
        found.add(attrs.get("cloud.region", {}).get("stringValue"))
    return found


def unpack(args: tuple) -> dict:
    corpus, part, seg, region = args
    needle = json.dumps(region).encode()
    entries = [json.loads(line) for line in (corpus / seg["path"].replace(".otlp.zst", ".idx.jsonl")).open()]
    dctx = zstandard.ZstdDecompressor()
    got = {"signal": seg["signal"], "seq": seg["seq"], "frames": 0, "records": 0, "kept_frames": 0}
    got |= {"kept_records": 0, "kept_bytes": 0, "dropped_checked": 0}
    part.parent.mkdir(parents=True, exist_ok=True)
    with (corpus / seg["path"]).open("rb") as f, part.open("wb") as out:
        for i, e in enumerate(entries):
            f.seek(e["offset"])
            body = dctx.decompress(f.read(e["length"]))
            if hashlib.sha256(body).hexdigest() != e["body_sha256"]:
                raise ValueError(f"{seg['path']}#{e['index']}: body sha256 mismatch")
            if b"\n" in body:
                raise ValueError(f"{seg['path']}#{e['index']}: body is not one line")
            got["frames"] += 1
            got["records"] += e["records"]
            if needle in body:
                out.write(body + b"\n")
                got["kept_frames"] += 1
                got["kept_records"] += e["records"]
                got["kept_bytes"] += len(body) + 1
            elif i % SAMPLE == 0:
                if region in regions(seg["signal"], body):
                    raise ValueError(f"{seg['path']}#{e['index']}: dropped a frame holding {region}")
                got["dropped_checked"] += 1
    if (got["frames"], got["records"]) != (seg["frames"], seg["records"]):
        raise ValueError(
            f"{seg['path']}: read {got['frames']} frames, {got['records']} records; segments.jsonl says "
            f"{seg['frames']}, {seg['records']}"
        )
    return got


def main(corpus: Path, out: Path, region: str, workers: int) -> int:
    segs = [json.loads(line) for line in (corpus / "segments.jsonl").open()]
    parts = out / ".parts"
    jobs = [(corpus, parts / s["signal"] / f"{s['stream']}-{s['seq']:06d}.jsonl", s, region) for s in segs]
    with ProcessPoolExecutor(workers) as pool:
        results = list(pool.map(unpack, jobs, chunksize=1))
    totals = {}
    for signal in SIGNALS:
        mine = sorted((p, r) for (_, p, s, _), r in zip(jobs, results, strict=True) if s["signal"] == signal)
        with (out / f"{signal}.jsonl").open("wb") as f:
            for p, _ in mine:
                with p.open("rb") as src:
                    while chunk := src.read(1 << 24):
                        f.write(chunk)
                p.unlink()
        totals[signal] = {k: sum(r[k] for _, r in mine) for k in results[0] if k not in ("signal", "seq")}
        totals[signal]["segments"] = len(mine)
    for d in sorted(parts.rglob("*"), reverse=True):
        d.rmdir()
    parts.rmdir()
    summary = {"corpus": str(corpus), "region": region, "signals": totals}
    (out / "unpack.json").write_text(json.dumps(summary, indent=1) + "\n")
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], int(sys.argv[4]) if len(sys.argv) > 4 else 4))
