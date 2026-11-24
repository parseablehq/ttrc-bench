"""Two clocks that must never mix.

EventNs is recording time, as stored in the archive and the reference store; truth is in EventNs.
WallNs is run time, what the stacks hold and the agent sees: WallNs = EventNs + shift.
The shift is a whole number of minutes (0 for live ingestion), so minute-precision answers survive it.
Rendering for the agent and grading are the only places the shift is applied.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, NewType

EventNs = NewType("EventNs", int)
WallNs = NewType("WallNs", int)

NS = 1_000_000_000
Precision = Literal["min", "s"]


@dataclass(frozen=True, slots=True)
class Shift:
    seconds: int

    def __post_init__(self) -> None:
        if self.seconds % 60:
            raise ValueError(f"shift must be whole minutes, got {self.seconds}s")

    def wall(self, t: EventNs) -> WallNs:
        return WallNs(t + self.seconds * NS)

    def event(self, t: WallNs) -> EventNs:
        return EventNs(t - self.seconds * NS)

    def wall_window(self, w: Window) -> WallWindow:
        return WallWindow(self.wall(w.start), self.wall(w.end))


@dataclass(frozen=True, slots=True)
class Window:
    """Half-open [start, end) in recording time."""

    start: EventNs
    end: EventNs

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError(f"empty window [{self.start}, {self.end})")

    def pad(self, before_s: int, after_s: int) -> Window:
        return Window(EventNs(self.start - before_s * NS), EventNs(self.end + after_s * NS))

    def iou(self, other: Window) -> float:
        inter = max(0, min(self.end, other.end) - max(self.start, other.start))
        union = max(self.end, other.end) - min(self.start, other.start)
        return inter / union

    def contains(self, t: int) -> bool:
        return self.start <= t < self.end


@dataclass(frozen=True, slots=True)
class WallWindow:
    start: WallNs
    end: WallNs


def render_utc(t: int, precision: Precision) -> str:
    """'2026-10-04T08:14Z' or '2026-10-04T08:14:12Z'. Always a full date, because corpora span days."""
    dt = datetime.fromtimestamp(t // NS, UTC)
    return dt.strftime("%Y-%m-%dT%H:%MZ" if precision == "min" else "%Y-%m-%dT%H:%M:%SZ")


def parse_utc(text: str) -> WallNs:
    """ISO-8601 UTC with a date, to the second. Time-only answers are rejected, never guessed."""
    s = text.strip()
    if not s.endswith("Z"):
        raise ValueError(f"timestamp must be UTC with a trailing Z: {text!r}")
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%MZ"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
        return WallNs(int(dt.timestamp()) * NS)
    raise ValueError(f"timestamp must look like 2026-10-04T08:14Z or 2026-10-04T08:14:12Z: {text!r}")
