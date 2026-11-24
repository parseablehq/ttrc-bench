"""Live tail: a writer appends the corpus-mini frames over time, with rotation, while readers follow the archive.

Evidence: tests/e2e/artifacts/archive_follow.json.
"""

import hashlib
import threading
import time
from pathlib import Path

from conftest import write_artifact

from otb.archive import Archive, ArchiveWriter, Header
from otb.normalise import normalise

REPO = Path(__file__).resolve().parents[2]
MINI = REPO / "tests/fixtures/corpus-mini"


def test_follow_reads_every_frame_once_while_writing(tmp_path: Path, artifacts: Path) -> None:
    src = Archive.open(MINI, verify="index")
    frames = [(ref.signal, src.read(ref, e)) for ref, e in src.merged()]
    m = src.manifest
    header = Header("follow", m.instances, m.source, m.normaliser, rotate_bytes=300_000)
    out = tmp_path / "live"
    writer = ArchiveWriter(out, header, rotate_s=None)

    def write() -> None:
        for i, (signal, body) in enumerate(frames):
            writer.append(signal, normalise(signal, body), received_ns=time.time_ns())
            if i % 20 == 0:
                time.sleep(0.02)
        writer.seal(scenario_sha256=None, schedule_sha256=None)
        writer.close()

    t = threading.Thread(target=write)
    t.start()
    live = Archive(out)
    followed = list(live.merged(follow=True, hold_s=0.05, poll_s=0.005))
    traces_followed = list(Archive(out).frames("traces", "g0", follow=True, poll_s=0.005))
    t.join()

    sealed = Archive.open(out, verify="full")
    after_seal = list(sealed.merged())
    want = sorted(hashlib.sha256(body).hexdigest() for _, body in frames)
    got = sorted(e.body_sha256 for _, e in followed)
    per_stream_in_order = all(
        [r for r, _ in followed if r.signal == s] == sorted(r for r, _ in followed if r.signal == s)
        for s in ("traces", "logs", "metrics")
    )
    result = {
        "frames_source": len(frames),
        "frames_followed": len(followed),
        "distinct_refs_followed": len({r for r, _ in followed}),
        "bodies_match": got == want,
        "segments_written": len(sealed.segments()),
        "traces_frames_followed": len(traces_followed),
        "per_stream_storage_order": per_stream_in_order,
        "read_back_equal": all(
            sealed.read(r) == src.read(sr) for (r, _), (sr, _) in zip(after_seal, src.merged(), strict=True)
        ),
    }
    write_artifact(artifacts / "archive_follow.json", result)

    assert result == {
        "frames_source": 232,
        "frames_followed": 232,
        "distinct_refs_followed": 232,
        "bodies_match": True,
        "segments_written": result["segments_written"],
        "traces_frames_followed": 90,
        "per_stream_storage_order": True,
        "read_back_equal": True,
    }
    assert result["segments_written"] > 3, "rotation must have produced several segments per signal"
