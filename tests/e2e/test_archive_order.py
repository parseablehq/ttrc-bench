"""Two archive properties the feeders rely on, on corpus-mini rewritten as two streams with frames out of order:

1. Archive.merged() yields every frame once, in global due order (max_event_ns, then the storage address), even
   when a stream holds a frame that arrived after frames due later, across segment boundaries.
2. corpus_id is a function of content: two writers appending the same segments in a different order give the
   same corpus_id, although their segments.jsonl differ.

Evidence: tests/e2e/artifacts/archive_order.json.
"""

from itertools import pairwise
from pathlib import Path

from conftest import write_artifact

from otb.archive import Archive, ArchiveWriter, Header, _merge_key
from otb.normalise import normalise

REPO = Path(__file__).resolve().parents[2]
MINI = REPO / "tests/fixtures/corpus-mini"


def late_arrivals(frames: list, every: int, by: int) -> list:
    """Every `every`-th frame arrives `by` frames late, as a slow exporter's retry would."""
    out = list(frames)
    for i in range(len(frames) - by - 1, 0, -every):
        out.insert(i + by, out.pop(i))
    return out


def write(root: Path, streams: dict[str, list], order: list[str], header: Header) -> Archive:
    for stream in order:
        w = ArchiveWriter(root, header, stream=stream, rotate_s=None, dedupe=False)
        for signal, body in streams[stream]:
            w.append(signal, normalise(signal, body), received_ns=0)
        w.seal_segments()
        w.close()
    w = ArchiveWriter(root, header, stream=order[0], rotate_s=None, dedupe=False)
    w.seal(scenario_sha256=None, schedule_sha256=None)
    w.close()
    return Archive.open(root, verify="full")


def storage_inversions(a: Archive) -> int:
    n = 0
    for signal, stream in a.streams():
        dues = [e.max_event_ns for _, e in a.frames(signal, stream)]
        n += sum(1 for x, y in pairwise(dues) if y < x)
    return n


def test_global_due_order_and_content_corpus_id(tmp_path: Path, artifacts: Path) -> None:
    src = Archive.open(MINI, verify="index")
    frames = [(ref.signal, src.read(ref, e)) for ref, e in src.merged()]
    streams = {"g0": late_arrivals(frames[0::2], 7, 15), "g1": late_arrivals(frames[1::2], 5, 9)}
    m = src.manifest
    header = Header("order", m.instances, m.source, m.normaliser, rotate_bytes=200_000)

    a = write(tmp_path / "a", streams, ["g0", "g1"], header)
    b = write(tmp_path / "b", streams, ["g1", "g0"], header)

    merged = list(a.merged())
    keys = [_merge_key(i) for i in merged]
    result = {
        "frames_source": len(frames),
        "frames_merged": len(merged),
        "distinct_refs": len({r for r, _ in merged}),
        "segments": len(a.segments()),
        "storage_order_inversions": storage_inversions(a),
        "merged_inversions": sum(1 for x, y in pairwise(keys) if y < x),
        "merged_is_sorted": keys == sorted(keys),
        "segments_jsonl_differ": (tmp_path / "a/segments.jsonl").read_bytes()
        != (tmp_path / "b/segments.jsonl").read_bytes(),
        "corpus_id_a": a.manifest.corpus_id,
        "corpus_id_b": b.manifest.corpus_id,
    }
    write_artifact(artifacts / "archive_order.json", result)

    assert result["frames_merged"] == result["distinct_refs"] == result["frames_source"] == 232
    assert result["storage_order_inversions"] > 20, "the fixture must hold frames out of due order"
    assert result["segments"] > 6, "rotation must spread late frames across segment boundaries"
    assert result["merged_inversions"] == 0
    assert result["merged_is_sorted"]
    assert result["segments_jsonl_differ"]
    assert result["corpus_id_a"] == result["corpus_id_b"]
