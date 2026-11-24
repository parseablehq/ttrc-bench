"""Frame preparation before and after U10, on every frame of a corpus: the bytes sent must be identical.

  uv run python tests/scale/prep_identity.py CORPUS [--shift-s 665880] [--before 3f74330]

Loads `otb/timeshift.py` as it was at --before (`git show`), and for every frame in due order compares the old and
new shifted body, rewrite counts and leftover recording-time count. Prints the frame count, the number that
differ (expected 0), a sha256 over each side's sent bytes, and each side's preparation seconds. The corpus is only
read. tests/e2e/test_replay_bodies.py is the offline version on corpus-mini.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
import types
from pathlib import Path

from otb import timeshift as new
from otb.archive import Archive

REPO = Path(__file__).resolve().parents[2]


def load_before(rev: str) -> types.ModuleType:
    source = subprocess.run(
        ["git", "-C", str(REPO), "show", f"{rev}:otb/timeshift.py"], check=True, capture_output=True, text=True
    ).stdout
    old = types.ModuleType("timeshift_before")
    sys.modules[old.__name__] = old
    exec(compile(source, f"{rev}:otb/timeshift.py", "exec"), old.__dict__)
    return old


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("corpus", type=Path)
    p.add_argument("--shift-s", type=int, default=665880)
    p.add_argument("--before", default="3f74330")
    a = p.parse_args()
    old = load_before(a.before)
    archive = Archive(a.corpus)
    span = archive.manifest.window
    days = new.recording_days(span.start, span.end)
    frames = differing = raw_bytes = 0
    digests = {"before": hashlib.sha256(), "after": hashlib.sha256()}
    seconds = {"before": 0.0, "after": 0.0}
    for ref, e in archive.merged():
        raw = archive.read(ref, e)
        raw_bytes += len(raw)
        got = {}
        for side, mod in (("before", old), ("after", new)):
            band = mod.Band.of(e.min_event_ns, e.max_event_ns)
            t0 = time.perf_counter()
            body, rewrites = mod.shift_body(raw, a.shift_s, band)
            left = mod.recording_time_left(body, days, band)
            seconds[side] += time.perf_counter() - t0
            digests[side].update(body)
            got[side] = (body, dict(rewrites), left)
        frames += 1
        differing += got["before"] != got["after"]
    doc = {
        "corpus_id": archive.manifest.corpus_id,
        "shift_s": a.shift_s,
        "before": a.before,
        "frames": frames,
        "raw_bytes": raw_bytes,
        "frames_differing": differing,
        "sent_sha256": {k: v.hexdigest() for k, v in digests.items()},
        "prep_s": {k: round(v, 2) for k, v in seconds.items()},
        "prep_mb_per_s": {k: round(raw_bytes / v / 1e6, 1) for k, v in seconds.items()},
    }
    print(json.dumps(doc, indent=1))
    return 0 if differing == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
