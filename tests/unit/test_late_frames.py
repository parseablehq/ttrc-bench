"""Late frames exclude only the tasks whose window holds their records, tested in isolation because every arrival-time
stack's exclusions read them.

Ways it could be wrong, written before the code:
 1. A late frame still excludes every task on its signal (its check has no window).
 2. The window stops at the frame's newest record, so a task window just after it, where the late-dated records
    actually sit, is not excluded.
 3. A late frame excludes tasks on another signal.
 4. A windowed late-frame check counts as covering a footprint, so an instance no stored-data check reaches runs.
"""

from __future__ import annotations

from otb.footprint import Footprint, Region, blocking, covered
from otb.gate import late_frame_checks
from otb.timebase import NS, EventNs, Window

SPAN = {"frame": "logs/g0/000000#4", "start_ns": 100 * NS, "end_ns": 112 * NS, "late_s": 11.2}


def fp(signal: str, start_s: int, end_s: int) -> Footprint:
    return Footprint((Region(signal, None, Window(EventNs(start_s * NS), EventNs(end_s * NS))),))  # type: ignore[arg-type]


def test_late_frame_excludes_only_overlapping_windows() -> None:
    checks = late_frame_checks("logs", 10.0, [SPAN])
    assert list(checks) == ["ingest.late:logs/g0/000000#4"]
    assert checks["ingest.late:logs/g0/000000#4"]["region"]["window"] == [100 * NS, 112 * NS]
    assert blocking(checks, fp("logs", 90, 101)) == ["ingest.late:logs/g0/000000#4"]
    assert blocking(checks, fp("logs", 111, 130)) == ["ingest.late:logs/g0/000000#4"], (
        "late-dated records after the frame"
    )
    assert blocking(checks, fp("logs", 112, 130)) == []
    assert blocking(checks, fp("logs", 0, 100)) == []
    assert blocking(checks, fp("traces", 90, 120)) == []


def test_late_frame_checks_cover_nothing() -> None:
    assert covered(late_frame_checks("logs", 10.0, [SPAN]), fp("logs", 90, 120)) is False
