"""Time strings inside the archive's bodies, found and rewritten by the replay shift (otb.timeshift).

Over every frame of corpus-mini (and the dry run when built): the in-band time strings per format before the
shift, the rewrites per format, and the recording-time strings left after it. Fails when a format is found but not
rewritten as often, or when anything is left. Evidence: tests/e2e/artifacts/time_strings.json.
"""

from collections import Counter
from pathlib import Path

from conftest import write_artifact

from otb.archive import Archive
from otb.timeshift import Band, recording_days, recording_time_left, shift_body, time_strings

REPO = Path(__file__).resolve().parents[2]
SHIFT_S = 8 * 86_400 + 7 * 60
CORPORA = {"corpus-mini": REPO / "tests/fixtures/corpus-mini", "dryrun": REPO / "corpora/dryrun"}


def count(root: Path) -> dict:
    archive = Archive(root)
    window = archive.manifest.window
    assert window is not None
    days = recording_days(window.start, window.end)
    found: Counter[str] = Counter()
    rewritten: Counter[str] = Counter()
    left = 0
    frames_with_time_strings = 0
    for ref, e in archive.merged():
        raw = archive.read(ref, e)
        band = Band.of(e.min_event_ns, e.max_event_ns)
        here = time_strings(raw, band)
        found += here
        frames_with_time_strings += bool(here)
        shifted, n = shift_body(raw, SHIFT_S, band)
        rewritten += n
        left += recording_time_left(shifted, days, band)
    return {
        "found": dict(sorted(found.items())),
        "rewritten": dict(sorted(rewritten.items())),
        "frames_with_time_strings": frames_with_time_strings,
        "recording_time_left": left,
    }


def test_every_in_band_time_string_is_rewritten(artifacts: Path) -> None:
    present = {name: root for name, root in CORPORA.items() if (root / "segments").exists()}
    got = {name: count(root) for name, root in present.items()}
    write_artifact(artifacts / "time_strings.json", {"shift_s": SHIFT_S} | got, sort_keys=True)
    for name, c in got.items():
        in_band = {k: v for k, v in c["found"].items() if not k.endswith("_out_of_band")}
        assert c["rewritten"] == in_band, name
        assert c["recording_time_left"] == 0, name
