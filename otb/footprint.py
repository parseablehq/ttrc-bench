"""Footprints: where in the data a gate check vouches, and where an instance's truth comes from.

A Region is one signal, a set of services, a recording-time window and a set of metric names; None on an axis
means unbounded (every service, all time, every metric). A gate check has one region (gate.json `region`), an
instance has a footprint of one or more (instances.jsonl `footprint`). A stack runs an instance unless a failed
or unverifiable check overlaps the footprint: same signal, services equal or either side every service, windows
intersecting, metric names equal when both sides name one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from otb.archive import SIGNALS, Signal
from otb.timebase import EventNs, Window

BLOCKING = ("fail", "unverifiable")


@dataclass(frozen=True, slots=True)
class Region:
    signal: Signal
    services: frozenset[str] | None = None
    window: Window | None = None
    metrics: frozenset[str] | None = None

    def __post_init__(self) -> None:
        if self.signal not in SIGNALS:
            raise ValueError(f"region signal {self.signal!r}")
        if self.services is not None and not self.services:
            raise ValueError("region with an empty service set: use None for every service")
        if self.metrics is not None and not self.metrics:
            raise ValueError("region with an empty metric set: use None for every metric")

    def to_json(self) -> dict:
        return {
            "signal": self.signal,
            "services": None if self.services is None else sorted(self.services),
            "window": None if self.window is None else [self.window.start, self.window.end],
            "metrics": None if self.metrics is None else sorted(self.metrics),
        }

    @staticmethod
    def from_json(d: Mapping) -> Region:
        w = d["window"]
        return Region(
            d["signal"],
            None if d["services"] is None else frozenset(d["services"]),
            None if w is None else Window(EventNs(w[0]), EventNs(w[1])),
            None if d["metrics"] is None else frozenset(d["metrics"]),
        )


@dataclass(frozen=True, slots=True)
class Footprint:
    regions: tuple[Region, ...]
    trace_ids: tuple[str, ...] = ()  # traces the truth reads by id; the gate checks each one directly

    def __post_init__(self) -> None:
        if not self.regions:
            raise ValueError("a footprint needs at least one region")

    def to_json(self) -> dict:
        return {"regions": [r.to_json() for r in self.regions], "trace_ids": list(self.trace_ids)}

    @staticmethod
    def from_json(d: Mapping) -> Footprint:
        return Footprint(tuple(Region.from_json(r) for r in d["regions"]), tuple(d.get("trace_ids", ())))


def _meet(a: frozenset[str] | None, b: frozenset[str] | None) -> bool:
    return a is None or b is None or not a.isdisjoint(b)


def overlaps(a: Region, b: Region) -> bool:
    if a.signal != b.signal or not _meet(a.services, b.services) or not _meet(a.metrics, b.metrics):
        return False
    if a.window is None or b.window is None:
        return True
    return max(a.window.start, b.window.start) < min(a.window.end, b.window.end)


def _hits(check: Mapping, fp: Footprint) -> bool:
    region = Region.from_json(check["region"])
    return any(overlaps(region, r) for r in fp.regions)


def blocking(checks: Mapping[str, Mapping], fp: Footprint) -> list[str]:
    """Ids of the failed or unverifiable checks that overlap the footprint, sorted."""
    return sorted(cid for cid, c in checks.items() if c["status"] in BLOCKING and _hits(c, fp))


def covered(checks: Mapping[str, Mapping], fp: Footprint) -> bool:
    """Whether a windowed check, whatever its status, overlaps the footprint. Ingest checks (ledger, refusal
    counters, late frames) read no stored data, so they cover nothing, windowed or not."""
    return any(
        c["region"]["window"] is not None and not str(c.get("family", "")).startswith("ingest.") and _hits(c, fp)
        for c in checks.values()
    )
