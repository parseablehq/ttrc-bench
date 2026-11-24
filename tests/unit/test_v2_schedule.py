"""synthesise_v2_schedule turns a v2 corpus.yaml's HH:MM:SS events into recording-time windows.

Ways it can fail, each asserted below:
- a capture that runs past midnight puts an event after midnight on the first day, a day early;
- an event inside the window on the first day is moved to the second;
- a time that fits no day of the window, or two days of a window over 24 h, is accepted;
- an event window outside the capture is accepted, so a task binds a window with no telemetry under it;
- a slice of a longer capture cannot keep a fault that the slice cut short.
"""

import pytest

from otb.scenario import DEFAULT_CATALOGUE, Catalogue, ScenarioError, synthesise_v2_schedule
from otb.timebase import parse_utc

CAT = Catalogue.load(DEFAULT_CATALOGUE)


def cfg(window: list[str], *events: dict) -> dict:
    return {"window_utc": window, "events": list(events)}


def fault(start: str, end: str) -> dict:
    return {"from": start, "to": end, "kind": "fault", "flag": "paymentFailure", "value": "50%"}


def windows(c: dict, start: str, end: str) -> list[tuple[int, int]]:
    _, s = synthesise_v2_schedule(c, CAT, parse_utc(start), parse_utc(end))
    return [(e.window.start, e.window.end) for e in s.entries + s.unplanned]


def test_events_after_midnight_roll_over_to_the_next_day() -> None:
    c = cfg(
        ["2026-09-24T23:30:00", "2026-09-25T00:40:00"],
        fault("23:50:00", "00:10:00"),
        {"at": "00:20:00", "kind": "load"},
    )
    assert windows(c, "2026-09-24T23:30:00Z", "2026-09-25T00:40:00Z") == [
        (parse_utc("2026-09-24T23:50:00Z"), parse_utc("2026-09-25T00:10:00Z")),
        (parse_utc("2026-09-25T00:20:00Z"), parse_utc("2026-09-25T00:40:00Z")),
    ]


def test_a_same_day_capture_keeps_its_day() -> None:
    c = cfg(["2026-09-24T07:46:00", "2026-09-24T08:27:00"], fault("08:14:12", "08:19:19"))
    assert windows(c, "2026-09-24T07:46:27Z", "2026-09-24T08:26:45Z") == [
        (parse_utc("2026-09-24T08:14:12Z"), parse_utc("2026-09-24T08:19:19Z"))
    ]


def test_a_time_outside_the_window_or_ambiguous_is_refused() -> None:
    with pytest.raises(ScenarioError, match="06:00:00 lies in no day of the window"):
        windows(
            cfg(["2026-09-24T07:46:00", "2026-09-24T08:27:00"], fault("06:00:00", "08:00:00")),
            "2026-09-24T07:46:00Z",
            "2026-09-24T08:27:00Z",
        )
    with pytest.raises(ScenarioError, match="12:00:00 fits more than one day"):
        windows(
            cfg(["2026-09-24T00:00:00", "2026-09-26T00:00:00"], fault("12:00:00", "13:00:00")),
            "2026-09-24T00:00:00Z",
            "2026-09-26T00:00:00Z",
        )


def test_a_window_outside_the_capture_is_refused() -> None:
    c = cfg(["2026-09-24T07:46:00", "2026-09-24T08:27:00"], fault("08:14:12", "08:26:59"))
    with pytest.raises(
        ScenarioError, match=r"f1 \[2026-09-24T08:14:12Z, 2026-09-24T08:26:59Z\) lies outside the capture"
    ):
        windows(c, "2026-09-24T07:46:27Z", "2026-09-24T08:26:45Z")


def test_a_slice_may_cut_a_window_short() -> None:
    c = cfg(["2026-09-24T08:13:00", "2026-09-24T08:16:00"], fault("08:14:12", "08:19:19")) | {"slice_of": {}}
    assert windows(c, "2026-09-24T08:13:00Z", "2026-09-24T08:16:00Z") == [
        (parse_utc("2026-09-24T08:14:12Z"), parse_utc("2026-09-24T08:19:19Z"))
    ]
