"""Record what a replay sends for every corpus-mini frame under several shifts: the oracle for frame preparation.

  uv run python tests/fixtures/make_replay_bodies.py > tests/fixtures/replay-bodies.json

Written once with the frame preparation of commit 3f74330, before U10 made it cheaper. The metrics entries were
refreshed for the public export, when host process and container names in corpus-mini were replaced by placeholders.
tests/e2e/test_replay_bodies.py checks that the current code produces the same bytes, rewrite counts and
recording-time leftovers. Do not regenerate it to make that test pass: a difference is a change in what the stacks
receive.
"""

import hashlib
import json
import sys
from pathlib import Path

from otb.archive import Archive
from otb.ingest import frame_name
from otb.timeshift import Band, recording_days, recording_time_left, shift_body, time_strings

REPO = Path(__file__).resolve().parents[2]
MINI = REPO / "tests/fixtures/corpus-mini"
# 0 sends the archive's bytes; 60 is too small for the leftover check to tell shifted from unshifted strings; 665880
# is the full dry-run session's shift; the negative shift moves dates back a year; 8_523_000 carries the slice's
# day-boundary strings across a new year.
SHIFTS = (0, 60, 665_880, -31_536_000, 8_523_000)
# Bodies the slice lacks: epoch attributes in band, and time strings packed against each other so that a matcher
# which resumes at the wrong offset would rewrite a different set. The band is the slice's.
EDGES = [
    b'{"intValue":"1790237612345","x":{"stringValue":"1790237612"},"y":{"stringValue":"1790237612345678901"}}',
    b'{"stringValue":"1790237612""stringValue":"1790237613"}',
    b'"stringValue":"1790237612"stringValue":"1790237613"',
    b"2026-09-24T08:14:42.1234-09-24T08:14:42Z 2026-09-24T08:14:42+0530-09-24T08:14:43",
    b"12026-09-24T08:14:42Z x2026-09-24 08:14:42,5 2026-09-24T08:14:421 2026-13-24T08:14:42Z",
    b"[2026-09-24T23:59:59.999Z] 2026-09-25T00:00:00-01:00 2026-09-23T23:00:00+14:00 2026-09-26T01:00:00Z",
    b'{"timeUnixNano":"0","startTimeUnixNano":"00","endTimeUnixNano":"1790237612000000000"}',
]
EDGE_BAND = (1790237400 * 10**9, 1790237580 * 10**9)


def record(archive: Archive, shift_s: int) -> dict:
    span = archive.manifest.window
    days = recording_days(span.start, span.end)
    frames = []
    for ref, e in archive.merged():
        raw = archive.read(ref, e)
        band = Band.of(e.min_event_ns, e.max_event_ns)
        body, rewrites = shift_body(raw, shift_s, band)
        frames.append(
            [
                frame_name(ref),
                hashlib.sha256(body).hexdigest(),
                dict(sorted(rewrites.items())),
                recording_time_left(body, days, band),
                dict(sorted(time_strings(raw, band).items())),
            ]
        )
    return {"shift_s": shift_s, "frames": frames}


def main() -> int:
    archive = Archive(MINI)
    span = archive.manifest.window
    days = recording_days(span.start, span.end)
    band = Band.of(*EDGE_BAND)
    edges = []
    for body in EDGES:
        for s in SHIFTS:
            out, rewrites = shift_body(body, s, band)
            edges.append(
                [
                    body.decode(),
                    s,
                    out.decode(),
                    dict(sorted(rewrites.items())),
                    recording_time_left(out, days, band),
                    dict(sorted(time_strings(body, band).items())),
                ]
            )
    doc = {
        "corpus_id": archive.manifest.corpus_id,
        "shifts": [record(archive, s) for s in SHIFTS],
        "edges": edges,
    }
    sys.stdout.write(json.dumps(doc, indent=0, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
