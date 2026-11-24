"""Field graders, tested in isolation because every outcome in a run passes through them.

Ways a field grader can be wrong, written before the code:
 1. A time answer without a date (HH:MM) is accepted and matched modulo a day (the v2 defect).
 2. A time on a different day but the same clock time grades correct.
 3. A truth time is shifted by the wrong amount, or not at all, before comparison.
 4. Tolerance is applied in the wrong unit (ms vs s) or not at all.
 5. A boolean True grades equal to integer 1 (Python bool is an int).
 6. An integer answer of 42.0 or "42" passes validation.
 7. Relative tolerance on a number is computed against the answer instead of the truth.
 8. An enum accepts a value outside its closed set.
 9. A string set grades correct when it lists extra items, or credit is non-zero when it misses a true item.
10. A list longer than its cap passes validation (shotgun answers).
11. An object list grades correct when one true item is missing or has a wrong non-key field (ITBench-AA: 0).
12. An object list credits duplicates of one true item as several matches.
13. An empty-truth object list (a negative control) grades correct for a non-empty answer.
14. A window with end before start passes validation; IoU is computed on unshifted truth.
15. Metric names in OTel and Prometheus spellings fail to match; URL paths with query strings fail to match.
16. A count map grades differently when it includes explicit zero entries.
17. A path (ordered) grades correct when the order differs; credit is not the matched prefix share.
18. null answers grade ok against a non-null truth.
19. An unknown field spec key (typo) is silently ignored.
20. An excerpt of an error text grades correct when it is a fragment that other errors in the window share
    ("request failed"), an unrelated text, empty, or a superset (the text wrapped in a caller's message, or every
    error text pasted together); or grades wrong when it is the core phrase, or the same error with another
    attribute value.
21. A window answer that gives the exact truth to the second loses IoU because the truth's end was truncated to
    the minute.
22. An object list is matched greedily, so the same items in another order grade differently.
23. null and an empty list grade as different answers, although the floor and the prompt treat them as one fact.
"""

from __future__ import annotations

import pytest

from otb.tasks.fields import (
    Mark,
    answer_errors,
    answer_tool,
    excerpt_truth,
    exemplar,
    grade,
    parse_field,
    parse_fields,
    to_wall,
)
from otb.timebase import NS, Shift

T0 = 1_790_000_000 * NS  # 2026-09-21T14:13:20Z
SHIFT = Shift(354_720)


def f(spec: dict, name: str = "x"):
    return parse_field(name, spec)


def test_time_requires_date():
    fld = f({"type": "time", "precision": "s", "tolerance_s": 1})
    assert answer_errors(parse_fields({"x": {"type": "time", "precision": "s", "tolerance_s": 1}}), {"x": "13:33:20"})
    assert grade(fld, "13:33:20Z", "2026-09-21T14:13:20Z").value_ok is False


def test_time_other_day_same_clock_is_wrong():
    fld = f({"type": "time", "precision": "s", "tolerance_s": 1})
    want = to_wall(fld, T0, Shift(0))
    assert want == "2026-09-21T14:13:20Z"
    assert grade(fld, "2026-09-22T14:13:20Z", want).value_ok is False


def test_time_shift_and_tolerance_in_seconds():
    fld = f({"type": "time", "precision": "s", "tolerance_s": 1})
    want = to_wall(fld, T0 + 400_000_000, SHIFT)
    assert want == "2026-09-25T16:45:20Z"
    assert grade(fld, "2026-09-25T16:45:21Z", want).value_ok is True
    assert grade(fld, "2026-09-25T16:45:22Z", want).value_ok is False
    assert grade(fld, "2026-09-21T14:13:20Z", want).value_ok is False


def test_minute_precision_truncates_truth():
    fld = f({"type": "time", "precision": "min", "tolerance_s": 0})
    assert to_wall(fld, T0 + 50 * NS, Shift(0)) == "2026-09-21T14:14Z"
    assert grade(fld, "2026-09-21T14:14Z", "2026-09-21T14:14Z").value_ok is True


def test_boolean_is_not_int():
    flds = parse_fields({"b": {"type": "boolean"}, "n": {"type": "integer"}})
    assert answer_errors(flds, {"b": 1, "n": 3})
    assert answer_errors(flds, {"b": True, "n": True})
    assert not answer_errors(flds, {"b": True, "n": 3})


def test_integer_rejects_float_and_string():
    flds = parse_fields({"n": {"type": "integer"}})
    assert answer_errors(flds, {"n": 42.0})
    assert answer_errors(flds, {"n": "42"})


def test_number_relative_tolerance_against_truth():
    fld = f({"type": "number", "tolerance_rel": 0.1})
    assert grade(fld, 109.0, 100.0).value_ok is True
    assert grade(fld, 111.0, 100.0).value_ok is False
    assert grade(fld, 90.5, 100.0).value_ok is True
    assert grade(fld, 89.0, 100.0).value_ok is False


def test_enum_closed():
    flds = parse_fields({"c": {"type": "enum", "values": ["none", "latency"]}})
    assert answer_errors(flds, {"c": "oom"})
    assert not answer_errors(flds, {"c": None})


def test_string_set_extra_and_missing():
    fld = f({"type": "string_set", "cap": 3, "grading": "precision_at_full_recall"})
    g = grade(fld, ["a", "b", "c"], ["a", "b"])
    assert (g.value_ok, g.credit) == (False, pytest.approx(2 / 3))
    g = grade(fld, ["a"], ["a", "b"])
    assert (g.value_ok, g.credit) == (False, 0.0)
    g = grade(fld, ["B ", "a"], ["a", "b"])
    assert (g.value_ok, g.credit) == (True, 1.0)
    exact = f({"type": "string_set", "cap": 3})
    assert grade(exact, ["a", "b", "c"], ["a", "b"]).credit == 0.0


def test_cap_enforced():
    flds = parse_fields({"s": {"type": "string_set", "cap": 2}})
    assert answer_errors(flds, {"s": ["a", "b", "c"]})


L5 = {
    "type": "object_list",
    "cap": 3,
    "item": {
        "service": {"type": "string"},
        "fault_class": {"type": "enum", "values": ["latency", "error", "none"]},
        "onset_at": {"type": "time", "precision": "min", "tolerance_s": 120},
    },
}


def test_object_list_precision_at_full_recall():
    fld = f(L5)
    truth = [
        {"service": "payment", "fault_class": "error", "onset_at": T0},
        {"service": "cart", "fault_class": "latency", "onset_at": T0 + 600 * NS},
    ]
    want = to_wall(fld, truth, Shift(0))
    assert want[0] == {"service": "payment", "fault_class": "error", "onset_at": "2026-09-21T14:13Z"}
    both = [dict(w) for w in want]
    assert grade(fld, both, want).value_ok is True
    extra = [*both, {"service": "ad", "fault_class": "error", "onset_at": "2026-09-21T14:13Z"}]
    g = grade(fld, extra, want)
    assert (g.value_ok, g.credit) == (False, pytest.approx(2 / 3))
    assert grade(fld, both[:1], want).credit == 0.0
    late = [both[0], {**both[1], "onset_at": "2026-09-21T14:40Z"}]
    assert grade(fld, late, want).credit == 0.0


def test_object_list_duplicates_do_not_double_count():
    fld = f(L5)
    want = to_wall(fld, [{"service": "payment", "fault_class": "error", "onset_at": T0}], Shift(0))
    dup = [dict(want[0]), dict(want[0])]
    g = grade(fld, dup, want)
    assert (g.value_ok, g.credit) == (False, 0.5)


def test_object_list_empty_truth_control():
    fld = f(L5)
    assert grade(fld, [], []).value_ok is True
    one = [{"service": "payment", "fault_class": "error", "onset_at": "2026-09-21T14:13Z"}]
    g = grade(fld, one, [])
    assert (g.value_ok, g.credit) == (False, 0.0)


def test_window_validation_and_iou():
    flds = parse_fields({"w": {"type": "window", "precision": "min", "min_iou": 0.5}})
    assert answer_errors(flds, {"w": {"start": "2026-09-21T13:40Z", "end": "2026-09-21T13:30Z"}})
    fld = flds[0]
    want = to_wall(fld, [T0, T0 + 600 * NS], SHIFT)
    assert want == {"start": "2026-09-25T16:45Z", "end": "2026-09-25T16:56Z"}
    g = grade(fld, {"start": "2026-09-25T16:45Z", "end": "2026-09-25T17:00Z"}, want)
    assert (g.value_ok, g.credit) == (True, pytest.approx(11 / 15))
    assert grade(fld, {"start": "2026-09-21T14:13Z", "end": "2026-09-21T14:23Z"}, want).credit == 0.0


def test_metric_and_url_path_normalisation():
    metric = f({"type": "string", "match": "metric_name"})
    assert grade(metric, "traces_span_metrics_calls_total", "traces.span.metrics.calls").value_ok is True
    path = f({"type": "string", "match": "url_path"})
    assert grade(path, "/api/cart/?x=1", "/api/cart").value_ok is True
    contains = f({"type": "string", "match": "contains"})
    assert grade(contains, "error: Invalid Token here", "invalid token").value_ok is True
    exact = f({"type": "string"})
    assert grade(exact, "paymentx", "payment").value_ok is False


SIGNATURE = "Payment request failed. Invalid token. demo.user_context.loyalty_level=gold"
# Other distinct error texts of the dry run's fault window, abridged. The second wraps the signature and the third
# is the same error for another user, so neither counts against it.
WINDOW_ERRORS = [
    "API request failed",
    "failed to charge card: could not charge the card: rpc error: code = Unknown desc = " + SIGNATURE,
    SIGNATURE.replace("gold", "silver"),
    "Error scraping metrics",
    "connect ECONNREFUSED 192.168.117.16:3550",
]


def test_error_text_excerpt():
    fld = f({"type": "string", "match": "excerpt"})
    want = excerpt_truth(SIGNATURE, WINDOW_ERRORS)
    assert want == {
        "text": SIGNATURE,
        "tokens": ["demo", "invalid", "loyalty_level", "payment", "token", "user_context"],
    }
    assert grade(fld, "Invalid token", want).value_ok is True
    assert grade(fld, "payment request failed.  INVALID TOKEN.", want).value_ok is True
    assert grade(fld, "Payment request", want).value_ok is True
    assert grade(fld, SIGNATURE.replace("gold", "platinum"), want).value_ok is True
    assert grade(fld, "request failed", want).value_ok is False
    assert grade(fld, WINDOW_ERRORS[1], want).value_ok is False
    assert grade(fld, " | ".join([SIGNATURE, *WINDOW_ERRORS]), want).value_ok is False
    assert grade(fld, "", want).value_ok is False
    assert grade(fld, "Invalid card number", want).value_ok is False
    shared = excerpt_truth(SIGNATURE, [*WINDOW_ERRORS, "Payment request timed out"])
    assert grade(fld, "Payment request", shared).value_ok is False
    assert grade(fld, "Invalid token", shared).value_ok is True
    assert exemplar(fld, want) == SIGNATURE


def test_excerpt_truth_without_distinguishing_token_is_empty():
    assert excerpt_truth("connection refused", ["connection was refused by peer"])["tokens"] == []


def test_count_map_zero_entries():
    fld = f({"type": "count_map"})
    assert grade(fld, {"a": 3, "b": 0}, {"a": 3}).value_ok is True
    assert grade(fld, {"a": 3, "c": 1}, {"a": 3}).value_ok is False


def test_path_order_and_prefix_credit():
    fld = f({"type": "path", "cap": 5})
    g = grade(fld, ["frontend", "checkout", "cart"], ["frontend", "checkout", "payment"])
    assert (g.value_ok, g.credit) == (False, pytest.approx(2 / 3))
    assert grade(fld, ["checkout", "frontend"], ["frontend", "checkout"]).value_ok is False


def test_null_answer_is_not_ok():
    for spec, want in (({"type": "integer"}, 3), ({"type": "boolean"}, False), (L5, [{"service": "a"}])):
        g = grade(f(spec), None, want)
        assert (g.value_ok, g.credit) == (False, 0.0)


def test_null_and_empty_collection_are_one_answer():
    for spec, empty in ((L5, []), ({"type": "string_set", "cap": 3}, []), ({"type": "count_map"}, {})):
        assert grade(f(spec), None, empty) == grade(f(spec), empty, empty) == grade(f(spec), empty, None)
        assert grade(f(spec), None, empty).value_ok is True
    assert grade(f({"type": "string_set", "cap": 3}), None, ["a"]).value_ok is False


def test_window_rounds_start_down_and_end_up():
    fld = f({"type": "window", "precision": "min", "min_iou": 0.5})
    want = to_wall(fld, [T0 + 22 * NS, T0 + 305 * NS], Shift(0))
    assert want == {"start": "2026-09-21T14:13Z", "end": "2026-09-21T14:19Z"}
    exact = {"start": "2026-09-21T14:13:42Z", "end": "2026-09-21T14:18:25Z"}
    assert grade(fld, exact, want) == Mark(True, 1.0)
    on_the_minute = to_wall(fld, [T0 - 20 * NS, T0 + 220 * NS], Shift(0))
    assert on_the_minute == {"start": "2026-09-21T14:13Z", "end": "2026-09-21T14:17Z"}


def test_object_list_matching_is_optimal_and_order_free():
    fld = f({**L5, "item": {**L5["item"], "onset_at": {"type": "time", "precision": "s", "tolerance_s": 120}}})
    want = to_wall(
        fld,
        [
            {"service": "payment", "fault_class": "error", "onset_at": T0},
            {"service": "payment", "fault_class": "error", "onset_at": T0 + 180 * NS},
        ],
        Shift(0),
    )
    near_both = {"service": "payment", "fault_class": "error", "onset_at": "2026-09-21T14:14:50Z"}
    near_first = {"service": "payment", "fault_class": "error", "onset_at": "2026-09-21T14:13:20Z"}
    assert grade(fld, [near_both, near_first], want) == Mark(True, 1.0)
    assert grade(fld, [near_first, near_both], want) == Mark(True, 1.0)


def test_unknown_spec_key_rejected():
    with pytest.raises(ValueError, match="tolerence"):
        f({"type": "integer", "tolerence": 1})


def test_answer_tool_schema_is_closed():
    flds = parse_fields({"n": {"type": "integer"}, "causes": L5})
    tool = answer_tool(flds)
    schema = tool["inputSchema"]
    assert tool["name"] == "submit_answer"
    assert schema["required"] == ["n", "causes"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["causes"]["maxItems"] == 3
    assert answer_errors(flds, {"n": 1}) == ["missing field causes"]
    assert answer_errors(flds, {"n": 1, "causes": [], "extra": 2}) == ["unknown field extra"]
