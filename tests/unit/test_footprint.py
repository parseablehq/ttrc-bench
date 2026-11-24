"""The footprint-overlap rule that decides which stack runs which instance, tested in isolation because every
exclusion in a run passes through it.

Ways the rule can be wrong, written before the code:
 1. A failed check on another signal excludes the instance (a logs failure drops a traces-only task).
 2. A check over every service (service none) does not overlap a footprint that names one service.
 3. A `*` footprint (services unknown) does not overlap a check on one service.
 4. Checks on a service the footprint does not name overlap it.
 5. Half-open windows that only touch ([a, b) and [b, c)) overlap.
 6. A check with no window (ledger, refusal counters) does not overlap every window.
 7. Different metric names overlap; a check without a metric name (MetricNames) does not overlap a footprint
    that names one; a footprint without metric names does not overlap a series check.
 8. A passing check excludes; an unverifiable one does not.
 9. Only the first region of a footprint is consulted.
10. The reason lists checks in a run-dependent order, or lists a check twice.
11. An empty service or metric set (which would mean "nothing") parses, and then never or always matches.
12. A JSON round trip turns `*` (null) into an empty set or the reverse.
13. A footprint that no check covers at all counts as clean: a stale gate would let every instance run.
14. A ledger or counter check (no window) counts as covering a footprint, although it reads no stored data:
    found on the live gate, where it made rule 13 vacuous.
"""

from __future__ import annotations

import pytest

from otb.footprint import Footprint, Region, blocking, covered, overlaps
from otb.timebase import EventNs, Window

M = 60_000_000_000


def w(a: int, b: int) -> Window:
    return Window(EventNs(a * M), EventNs(b * M))


def check(status: str, signal: str, service: str | None, window: Window | None, metric: str | None = None) -> dict:
    region = Region(
        signal,  # type: ignore[arg-type]
        None if service is None else frozenset({service}),
        window,
        None if metric is None else frozenset({metric}),
    )
    return {"status": status, "region": region.to_json()}


PAYMENT_LOGS = Footprint((Region("logs", frozenset({"payment"}), w(10, 20)),))


def test_signal_must_match() -> None:
    assert not overlaps(Region("traces", None, None), Region("logs", None, None))
    checks = {"spans": check("fail", "traces", "payment", w(10, 20))}
    assert blocking(checks, PAYMENT_LOGS) == []


def test_services_overlap_when_equal_or_either_side_is_every_service() -> None:
    one = Region("logs", frozenset({"payment"}), w(0, 1))
    assert overlaps(Region("logs", None, w(0, 1)), one)
    assert overlaps(one, Region("logs", None, w(0, 1)))
    assert overlaps(Region("logs", frozenset({"cart", "payment"}), w(0, 1)), one)
    assert not overlaps(Region("logs", frozenset({"currency", "shipping"}), w(0, 1)), one)


def test_half_open_windows_that_touch_do_not_overlap() -> None:
    assert not overlaps(Region("logs", None, w(0, 10)), Region("logs", None, w(10, 20)))
    assert not overlaps(Region("logs", None, w(20, 30)), Region("logs", None, w(10, 20)))
    assert overlaps(Region("logs", None, w(0, 11)), Region("logs", None, w(10, 20)))
    assert overlaps(Region("logs", None, w(12, 13)), Region("logs", None, w(10, 20)))


def test_a_check_without_window_overlaps_every_window() -> None:
    assert overlaps(Region("logs", None, None), Region("logs", frozenset({"payment"}), w(10, 20)))
    checks = {"ingest.ledger:logs": check("fail", "logs", None, None)}
    assert blocking(checks, PAYMENT_LOGS) == ["ingest.ledger:logs"]


def test_metric_names() -> None:
    calls = Region("metrics", frozenset({"frontend"}), w(0, 5), frozenset({"traces.span.metrics.calls"}))
    disk = Region("metrics", frozenset({"frontend"}), w(0, 5), frozenset({"system.disk.io"}))
    any_metric = Region("metrics", None, w(0, 5), None)
    assert not overlaps(disk, calls)
    assert overlaps(calls, calls)
    assert overlaps(any_metric, calls)
    assert overlaps(calls, Region("metrics", frozenset({"frontend"}), w(0, 5), None))


def test_status_decides_and_reasons_are_sorted_and_unique() -> None:
    checks = {
        "b": check("unverifiable", "logs", None, w(15, 16)),
        "a": check("fail", "logs", "payment", w(19, 21)),
        "c": check("pass", "logs", "payment", w(10, 20)),
        "d": check("fail", "logs", "currency", w(10, 20)),
    }
    fp = Footprint((Region("logs", frozenset({"payment"}), w(10, 20)), Region("logs", None, w(10, 20))))
    assert blocking(checks, fp) == ["a", "b", "d"]
    assert blocking(checks, PAYMENT_LOGS) == ["a", "b"]


def test_any_region_of_the_footprint_can_be_hit() -> None:
    fp = Footprint(
        (Region("traces", frozenset({"payment"}), w(0, 5)), Region("logs", frozenset({"payment"}), w(30, 40)))
    )
    assert blocking({"x": check("fail", "logs", "payment", w(35, 36))}, fp) == ["x"]


def test_empty_sets_are_refused() -> None:
    with pytest.raises(ValueError):
        Region("logs", frozenset(), None)
    with pytest.raises(ValueError):
        Region("metrics", None, None, frozenset())
    with pytest.raises(ValueError):
        Footprint(())
    with pytest.raises(ValueError):
        Region.from_json({"signal": "logs", "services": [], "window": None, "metrics": None})
    with pytest.raises(ValueError):
        Region.from_json({"signal": "events", "services": None, "window": None, "metrics": None})


def test_json_round_trip_keeps_every_service_distinct_from_named_services() -> None:
    fp = Footprint(
        (Region("logs", None, w(1, 2)), Region("metrics", frozenset({"b", "a"}), None, frozenset({"m"}))),
        ("abc",),
    )
    raw = fp.to_json()
    assert raw == {
        "regions": [
            {"signal": "logs", "services": None, "window": [1 * M, 2 * M], "metrics": None},
            {"signal": "metrics", "services": ["a", "b"], "window": None, "metrics": ["m"]},
        ],
        "trace_ids": ["abc"],
    }
    assert Footprint.from_json(raw) == fp


def test_a_footprint_no_check_covers_is_not_clean() -> None:
    checks = {"x": check("pass", "traces", None, w(10, 20))}
    assert not covered(checks, PAYMENT_LOGS)
    assert covered({"y": check("pass", "logs", "payment", w(0, 11))}, PAYMENT_LOGS)
    assert not covered({"z": check("fail", "logs", None, None)}, PAYMENT_LOGS)
    assert blocking({"z": check("fail", "logs", None, None)}, PAYMENT_LOGS) == ["z"]
