"""A failed count over every service narrowed to the services whose own counts failed, tested in isolation because
the narrowed region decides which instances a stack still runs. Moved here from tests/e2e/test_gate_exclusions.py,
whose dry-run fixture had a real case; the live-45b fixture that replaced it has none.

Ways it could be wrong, written before the code:
 1. The total is narrowed although one service's count of its window is missing from the plan, so the difference
    may sit in that service and an instance on it runs on a stack that lost its records.
 2. The total is narrowed to every sibling, or to the passing ones, instead of the failing ones.
 3. It is narrowed although the per-service counts do not add up to the total on the stack's side (records under
    a service the plan does not list), so the stack's extra or missing records are blamed on the wrong services.
 4. A sibling whose probe errored (no count) is taken as passing, and the total narrowed without it.
 5. Counts of another window, or of errors only, are taken as siblings of the total.
 6. A narrowed total still blocks an instance on a passing service, or no longer blocks one on a failing service.
"""

from __future__ import annotations

from otb.footprint import Footprint, Region, blocking
from otb.gate import Plan, Point, check_region, localise
from otb.stacks.contract import CountLogs, CountSpans
from otb.timebase import NS, EventNs, Window

W = Window(EventNs(60 * NS), EventNs(120 * NS))
OTHER = Window(EventNs(120 * NS), EventNs(180 * NS))


def point(service: str | None, *, error_only: bool = False, w: Window = W) -> Point:
    return Point.of(CountLogs(service, error_only), w)


def judge(counts: dict[Point, tuple[int, int | None]]) -> tuple[Plan, dict[str, dict]]:
    """Checks as judge_stack writes them: pass when the stack's count equals the reference count."""
    checks = {
        p.id: {
            "family": p.family,
            "signal": check_region(p.check, p.window).signal,
            "expected": want,
            "got": got,
            "status": "pass" if got == want else "fail",
            "region": check_region(p.check, p.window).to_json(),
        }
        for p, (want, got) in counts.items()
    }
    return Plan(0, "s", "c", {}, tuple(counts)), checks


TOTAL, CURRENCY, SHIPPING, PAYMENT = point(None), point("currency"), point("shipping"), point("payment")
LOST_TWO = {TOTAL: (30, 28), CURRENCY: (10, 9), SHIPPING: (8, 7), PAYMENT: (12, 12)}


def on(service: str) -> Footprint:
    return Footprint((Region("logs", frozenset({service}), W),))


def localised(counts: dict[Point, tuple[int, int | None]]) -> dict:
    plan, checks = judge(counts)
    localise(plan, checks)
    return checks[TOTAL.id]


def test_a_total_is_narrowed_to_its_failing_services() -> None:
    plan, checks = judge(LOST_TWO)
    localise(plan, checks)
    total = checks[TOTAL.id]
    assert (total["localised_to"], total["region"]["services"]) == (["currency", "shipping"], ["currency", "shipping"])
    assert blocking(checks, on("payment")) == []
    assert blocking(checks, on("currency")) == [CURRENCY.id, TOTAL.id]


def test_a_total_stays_every_service_without_every_sibling() -> None:
    plan, checks = judge({p: c for p, c in LOST_TWO.items() if p != PAYMENT})
    localise(plan, checks)
    assert "localised_to" not in checks[TOTAL.id]
    assert checks[TOTAL.id]["region"]["services"] is None
    assert blocking(checks, on("payment")) == [TOTAL.id], "the missing sibling may hold the difference"


def test_a_total_stays_every_service_when_its_siblings_do_not_add_up() -> None:
    unlisted = LOST_TWO | {TOTAL: (30, 31)}  # the stack holds 3 records under a service the plan does not count
    assert localised(unlisted)["region"]["services"] is None
    errored = LOST_TWO | {PAYMENT: (12, None)}
    assert localised(errored)["region"]["services"] is None, "a sibling without a count is not a passing sibling"


def test_only_counts_of_the_same_window_and_filter_are_siblings() -> None:
    elsewhere = {p: c for p, c in LOST_TWO.items() if p != PAYMENT} | {point("payment", w=OTHER): (12, 12)}
    assert localised(elsewhere)["region"]["services"] is None
    errors = {p: c for p, c in LOST_TWO.items() if p != PAYMENT} | {point("payment", error_only=True): (12, 12)}
    assert localised(errors)["region"]["services"] is None


def test_span_counts_are_narrowed_the_same_way() -> None:
    total, cart, ad = (Point.of(CountSpans(s, False), W) for s in (None, "cart", "ad"))
    plan, checks = judge({total: (20, 19), cart: (15, 14), ad: (5, 5)})
    localise(plan, checks)
    assert checks[total.id]["localised_to"] == ["cart"]
