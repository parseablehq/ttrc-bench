"""Replay's string-time rewriter (otb.timeshift), in isolation.

Ways it can fail, each asserted below:
- an envoy access-log time keeps its recording value, or loses its brackets, milliseconds or `Z`;
- a time with a zone offset is moved in UTC but rendered with the wrong offset, or a shift across a month or year
  boundary renders an impossible date;
- a space separator or a comma fraction is normalised to `T` or `.`;
- a build date far from the frame (kernel, runtime) is moved as if it were recording time;
- an epoch-millisecond attribute keeps its recording value, or a digit string that is not a time is moved;
- a zero `*UnixNano` becomes the shift, or a non-time like `2026-13-45T99:00:00` is parsed and rewritten;
- the leftover check misses a date spelling the rewriter does not know, or counts a shifted string;
- a shift too small to tell recording days from replay days is treated as checkable.
"""

from otb.timeshift import Band, recording_days, recording_time_left, shift_body, shift_clears, time_strings

NS = 1_000_000_000
T0 = 1790237682 * NS  # 2026-09-24T08:14:42Z
BAND = Band.of(T0, T0 + 5 * NS)
SHIFT = 8 * 86_400 + 7 * 60


def test_envoy_line_moves_with_its_spelling() -> None:
    body = b'{"stringValue":"[2026-09-24T08:14:42.123Z] \\"GET /api/cart HTTP/1.1\\" 200"}'
    got, n = shift_body(body, SHIFT, BAND)
    assert got == b'{"stringValue":"[2026-10-02T08:21:42.123Z] \\"GET /api/cart HTTP/1.1\\" 200"}'
    assert dict(n) == {"iso8601": 1}


def test_offset_space_and_comma_spellings_survive_a_year_rollover() -> None:
    band = Band.of(1798761540 * NS, 1798761540 * NS)  # 2026-12-31T23:59:00Z
    body = b"a 2027-01-01 01:59:00,5+02:00 b 2026-12-31T23:59:00Z c 2026-12-31 23:59:00"
    got, n = shift_body(body, 120, band)
    assert got == b"a 2027-01-01 02:01:00,5+02:00 b 2027-01-01T00:01:00Z c 2027-01-01 00:01:00"
    assert dict(n) == {"iso8601": 3}


def test_build_dates_and_non_times_are_left_alone() -> None:
    body = (
        b'{"stringValue":"2026-08-07T03:48:40Z"} {"stringValue":"2026-13-45T99:00:00"} '
        b'{"intValue":"1790237682123"} {"stringValue":"4111111111111"} {"startTimeUnixNano":"0"}'
    )
    got, n = shift_body(body, SHIFT, BAND)
    assert got == (
        b'{"stringValue":"2026-08-07T03:48:40Z"} {"stringValue":"2026-13-45T99:00:00"} '
        b'{"intValue":"1790929302123"} {"stringValue":"4111111111111"} {"startTimeUnixNano":"0"}'
    )
    assert dict(n) == {"epoch_attr": 1}
    assert dict(time_strings(body, BAND)) == {"iso8601_out_of_band": 1, "epoch_attr": 1, "epoch_attr_out_of_band": 1}


def test_unix_nano_fields_move_and_a_zero_shift_changes_nothing() -> None:
    body = b'{"timeUnixNano":"1790237682000000000","observedTimeUnixNano":"0"}'
    assert shift_body(body, 60, BAND)[0] == b'{"timeUnixNano":"1790237742000000000","observedTimeUnixNano":"0"}'
    assert shift_body(body, 0, BAND) == (body, {})


def test_leftover_check_counts_every_unshifted_spelling_and_nothing_shifted() -> None:
    days = recording_days(T0, T0 + 60 * NS)
    raw = b'[2026-09-24T08:14:42.123Z] 24/Sep/2026:08:14:42 +0000 2026/09/24 {"intValue":"1790237682123"}'
    assert recording_time_left(raw, days, BAND) == 4
    shifted, _ = shift_body(raw, SHIFT, BAND)
    assert shifted == b'[2026-10-02T08:21:42.123Z] 24/Sep/2026:08:14:42 +0000 2026/09/24 {"intValue":"1790929302123"}'
    assert recording_time_left(shifted, days, BAND) == 2


def test_only_a_shift_clear_of_the_recording_days_is_checkable() -> None:
    assert shift_clears(SHIFT, T0, T0 + 41 * 60 * NS) is True
    assert shift_clears(86_400, T0, T0 + 41 * 60 * NS) is False
    assert shift_clears(0, T0, T0) is False
