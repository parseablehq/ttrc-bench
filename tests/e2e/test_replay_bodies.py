"""Frame preparation sends the same bytes as before U10 made it cheaper.

tests/fixtures/replay-bodies.json was written by tests/fixtures/make_replay_bodies.py with the code of commit
3f74330. Ways a faster preparation could change what a stack receives, each caught below:
- a `*UnixNano` value is moved differently, or a zero is no longer written as "0";
- an ISO or epoch time string is rewritten that was left alone before, or the reverse, including strings packed
  against each other where a matcher that resumes at the wrong offset finds a different set;
- the per-format rewrite counts or the leftover recording-time count in the ingest report change;
- a frame is skipped by a fast path that decides a frame needs no rewrite when it does.
"""

import hashlib
import json
from pathlib import Path

from otb.archive import Archive
from otb.ingest import frame_name
from otb.timeshift import Band, recording_days, recording_time_left, shift_body, time_strings

REPO = Path(__file__).resolve().parents[2]
MINI = REPO / "tests/fixtures/corpus-mini"
ORACLE = json.loads((REPO / "tests/fixtures/replay-bodies.json").read_text())


def test_every_frame_and_edge_is_prepared_as_before() -> None:
    archive = Archive(MINI)
    assert archive.manifest.corpus_id == ORACLE["corpus_id"]
    span = archive.manifest.window
    days = recording_days(span.start, span.end)
    frames = [(ref, e, archive.read(ref, e)) for ref, e in archive.merged()]
    for want in ORACLE["shifts"]:
        got = []
        for ref, e, raw in frames:
            band = Band.of(e.min_event_ns, e.max_event_ns)
            body, rewrites = shift_body(raw, want["shift_s"], band)
            got.append(
                [
                    frame_name(ref),
                    hashlib.sha256(body).hexdigest(),
                    dict(sorted(rewrites.items())),
                    recording_time_left(body, days, band),
                    dict(sorted(time_strings(raw, band).items())),
                ]
            )
        assert len(got) == 232
        mismatched = [g[0] for g, w in zip(got, want["frames"], strict=True) if g != w]
        assert mismatched == [], f"shift {want['shift_s']}"

    band = Band.of(1790237400 * 10**9, 1790237580 * 10**9)
    for body, shift_s, out, rewrites, left, found in ORACLE["edges"]:
        got_out, got_rewrites = shift_body(body.encode(), shift_s, band)
        assert (got_out.decode(), dict(got_rewrites)) == (out, rewrites), (body, shift_s)
        assert recording_time_left(got_out, days, band) == left, (body, shift_s)
        assert dict(time_strings(body.encode(), band)) == found, body
