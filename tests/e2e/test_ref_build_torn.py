"""A reference build killed mid-write leaves a torn last line in built.jsonl.

Ways that can fail, each asserted below:
- read_built raises on the torn line, so the store cannot be opened and the build cannot resume;
- the next build appends its first record to the torn fragment, which corrupts that record too.
"""

from pathlib import Path

from synthetic import NS, log, logs, metric, point, span, traces, write_corpus

from otb.ref.build import build, read_built, ref_dir
from otb.ref.store import ReferenceStore
from otb.timebase import EventNs, Window


def test_a_torn_built_line_is_ignored_and_dropped_on_rebuild(tmp_path: Path) -> None:
    t = 1790237400 * NS
    corpus = write_corpus(
        tmp_path / "torn",
        [
            traces("cart", span("a" * 32, "s1", None, "GET", 2, t, 10)),
            logs("cart", log(t, "x")),
            metric("cart", "up", "gauge", [point(t, 1)]),
        ],
    )
    ledger = ref_dir(corpus) / "built.jsonl"
    before = read_built(ref_dir(corpus))
    with ledger.open("ab") as f:
        f.write(b'{"signal": "traces", "stream"')
    assert read_built(ref_dir(corpus)) == before
    rebuilt = build(corpus, workers=1)
    assert (rebuilt.segments_built, rebuilt.segments_skipped) == (0, 3)
    assert ledger.read_bytes().endswith(b"}\n")
    w = Window(EventNs(t), EventNs(t + 60 * NS))
    assert ReferenceStore.open(corpus).run_measure("span_count", {"window": w}).one()["n"] == 1
