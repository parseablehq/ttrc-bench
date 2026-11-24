"""Replay's second clock: recording-time instants written inside string values, moved with the frame.

The feeder shifts every `*UnixNano` field. Telemetry also carries its time as text: envoy's access log line starts
`[2026-09-24T08:14:42.123Z]`, and the collector's own logs carry `scrape_timestamp` as an epoch-millisecond
integer attribute. A replay that left those at recording time would show an agent two clocks a week apart. Formats
found in the dry-run corpus (docs/v1/units/F1.md has the counts):

  iso8601      `YYYY-MM-DD[T ]HH:MM:SS[.frac][Z|±HH:MM]`, as envoy writes it
  epoch_attr   a stringValue or intValue that is all digits, read as s, ms, us or ns since the epoch

Only instants inside the frame's band (its event times padded by BAND_PAD_NS) are rewritten, so a kernel or
runtime build date in a resource attribute keeps its value. Every rewrite keeps the original spelling: separator,
fraction digits, zone text. One regex pass per format, so the cost is linear in the frame.

Each pass is a replay feeder's hot path, so each pattern starts with a literal that the regex engine can skip to:
ISO is found at its first dash and EPOCH at `Value":"`, with the text before the literal checked by a lookbehind.
`_iso_matches` and `_epoch_matches` yield exactly the matches, in the order, that the plain patterns' finditer
would; tests/e2e/test_replay_bodies.py holds the sent bytes to those of the plain patterns.
"""

from __future__ import annotations

import calendar
import re
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache

NS = 1_000_000_000
DAY_NS = 86_400 * NS
BAND_PAD_NS = DAY_NS
UNIX_NANO = re.compile(rb'(UnixNano":")(\d+)(")')
_ISO_TAIL = rb"(\d{2})-(\d{2})([T ])(\d{2}):(\d{2}):(\d{2})([.,]\d{1,9})?(Z|[+-]\d{2}:?\d{2})?(?![0-9])"
ISO = re.compile(rb"(?<![0-9])(\d{4})-" + _ISO_TAIL)
ISO_AT_DASH = re.compile(rb"-(?<=(?<![0-9])(\d{4})-)" + _ISO_TAIL)
EPOCH = re.compile(rb'("(?:stringValue|intValue)":")(\d{10}|\d{13}|\d{16}|\d{19})(")')
EPOCH_AT_VALUE = re.compile(rb'Value":"(?:(?<=("stringValue":"))|(?<=("intValue":")))(\d{10}|\d{13}|\d{16}|\d{19})(")')
EPOCH_SCALE = {10: NS, 13: 1_000_000, 16: 1_000, 19: 1}
FORMATS = ("iso8601", "epoch_attr")
DATE_SPELLINGS = ("%Y-%m-%d", "%Y/%m/%d", "%d/%b/%Y")


@dataclass(frozen=True, slots=True)
class Band:
    """Instants [lo, hi) that count as recording time for one frame."""

    lo: int
    hi: int

    @staticmethod
    def of(min_event_ns: int, max_event_ns: int) -> Band:
        lo = min_event_ns or max_event_ns
        return Band(lo - BAND_PAD_NS, max_event_ns + BAND_PAD_NS)

    def __contains__(self, ns: int) -> bool:
        return self.lo <= ns < self.hi


def _zone_s(zone: bytes | None) -> int | None:
    if not zone or zone == b"Z":
        return 0
    sign = -1 if zone[:1] == b"-" else 1
    digits = zone[1:].replace(b":", b"")
    return sign * (int(digits[:2]) * 3600 + int(digits[2:]) * 60)


def _iso_instant(m: re.Match[bytes]) -> int | None:
    y, mo, d, _, h, mi, s, frac, zone = m.groups()
    if not (1 <= int(mo) <= 12 and 1 <= int(d) <= 31 and int(h) < 24 and int(mi) < 60 and int(s) < 61):
        return None
    secs = calendar.timegm((int(y), int(mo), int(d), int(h), int(mi), int(s), 0, 0, 0)) - (_zone_s(zone) or 0)
    sub = int((frac[1:] + b"000000000")[:9]) if frac else 0
    return secs * NS + sub


def _render_iso(m: re.Match[bytes], shift_s: int) -> bytes:
    y, mo, d, sep, h, mi, s, frac, zone = m.groups()
    wall = calendar.timegm((int(y), int(mo), int(d), int(h), int(mi), int(s), 0, 0, 0)) + shift_s
    t = time.gmtime(wall)
    out = b"%04d-%02d-%02d%s%02d:%02d:%02d" % (t.tm_year, t.tm_mon, t.tm_mday, sep, t.tm_hour, t.tm_min, t.tm_sec)
    return out + (frac or b"") + (zone or b"")


def _epoch_ns(digits: bytes) -> int:
    return int(digits) * EPOCH_SCALE[len(digits)]


def _iso_matches(body: bytes) -> Iterator[re.Match[bytes]]:
    """ISO.finditer(body), found at the dash: the leftmost ISO match starting at or after pos is the leftmost
    ISO_AT_DASH match at or after pos + 4, and the two end at the same byte. Group 1 starts where ISO's match does."""
    pos = 0
    while m := ISO_AT_DASH.search(body, pos + 4):
        yield m
        pos = m.end()


def _epoch_matches(body: bytes) -> Iterator[tuple[int, bytes, bytes, int]]:
    """(start, prefix, digits, end) of each EPOCH.finditer(body) match, found at `Value":"`. A candidate whose
    prefix begins inside the previous match is one EPOCH would not see, so it is skipped."""
    pos = at = 0
    while m := EPOCH_AT_VALUE.search(body, at):
        g = 1 if m.group(1) is not None else 2
        if m.start(g) < pos:
            at = m.start() + 1
            continue
        yield m.start(g), m.group(g), m.group(3), m.end()
        pos = at = m.end()


def _shift_nanos(body: bytes, delta: int) -> bytes:
    parts = UNIX_NANO.split(body)  # [text, prefix, digits, quote, text, ...]
    parts[2::4] = [str(int(v) + delta).encode() if v.strip(b"0") else b"0" for v in parts[2::4]]
    return b"".join(parts)


def shift_body(body: bytes, shift_s: int, band: Band) -> tuple[bytes, Counter[str]]:
    """The frame as a replay sends it: every non-zero `*UnixNano` moved by shift_s (zero stays zero, v2's rule),
    and every in-band time string moved too. Returns the body and the rewrites per format."""
    rewrites: Counter[str] = Counter()
    if not shift_s:
        return body, rewrites
    delta = shift_s * NS
    body = _shift_nanos(body, delta)

    out, last = [], 0
    for m in _iso_matches(body):
        at = _iso_instant(m)
        if at is not None and at in band:
            rewrites["iso8601"] += 1
            out += (body[last : m.start(1)], _render_iso(m, shift_s))
            last = m.end()
    if out:
        body = b"".join([*out, body[last:]])

    out, last = [], 0
    for start, prefix, digits, end in _epoch_matches(body):
        if _epoch_ns(digits) in band:
            rewrites["epoch_attr"] += 1
            moved = str(int(digits) + delta // EPOCH_SCALE[len(digits)]).encode()
            out += (body[last:start], prefix, moved, b'"')
            last = end
    if out:
        body = b"".join([*out, body[last:]])
    return body, rewrites


def time_strings(body: bytes, band: Band) -> Counter[str]:
    """Time strings per format whose instant lies in the band: what shift_body rewrites. Out-of-band matches are
    counted under `<format>_out_of_band`, so a report shows what was deliberately left alone."""
    found: Counter[str] = Counter()
    for m in _iso_matches(body):
        at = _iso_instant(m)
        if at is not None:
            found["iso8601" if at in band else "iso8601_out_of_band"] += 1
    for _, _, digits, _ in _epoch_matches(body):
        found["epoch_attr" if _epoch_ns(digits) in band else "epoch_attr_out_of_band"] += 1
    return found


def recording_days(start_ns: int, end_ns: int) -> frozenset[bytes]:
    """Every spelling in DATE_SPELLINGS of each UTC day from a day before start_ns to a day after end_ns."""
    first, last = (start_ns - DAY_NS) // DAY_NS, (end_ns + DAY_NS) // DAY_NS
    return frozenset(
        time.strftime(f, time.gmtime(day * 86_400)).encode() for day in range(first, last + 1) for f in DATE_SPELLINGS
    )


def recording_time_left(shifted: bytes, days: frozenset[bytes], band: Band) -> int:
    """After a shift, the strings that still say recording time: recording-day dates in any DATE_SPELLINGS
    spelling, plus in-band epoch attributes. Expected 0. Independent of the ISO rewriter's grammar, so it also
    catches a date format the rewriter does not know. Only meaningful when the shift moves the band clear of
    `days`; see `shift_clears`."""
    n = sum(shifted.count(d) for gate, group in _by_month(days) if gate in shifted for d in group)
    return n + sum(1 for _, _, digits, _ in _epoch_matches(shifted) if _epoch_ns(digits) in band)


@lru_cache(maxsize=8)
def _by_month(days: frozenset[bytes]) -> tuple[tuple[bytes, tuple[bytes, ...]], ...]:
    """The days grouped under the month part of their spelling (`2026-09-`, `2026/09/`, `/Sep/2026`), which every
    day in the group contains: a body without the month holds none of its days, so one scan replaces many."""
    groups: dict[bytes, list[bytes]] = {}
    for d in sorted(days):
        groups.setdefault(d[:8] if d[4:5] in (b"-", b"/") else d[2:], []).append(d)
    return tuple((gate, tuple(group)) for gate, group in groups.items())


def shift_clears(shift_s: int, start_ns: int, end_ns: int) -> bool:
    """True when the shifted recording days and bands miss the unshifted ones, so recording_time_left can tell
    a shifted string from an unshifted one."""
    return abs(shift_s) * NS > (end_ns - start_ns) + 3 * DAY_NS
