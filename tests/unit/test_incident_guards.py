"""The guards that drop a task whose window holds a second incident, tested in isolation because they decide which
questions a suite may ask.

Ways it could be wrong, written before the code:
 1. A question about an entry operation is kept although another service originates a third of its failures.
 2. It is dropped although the bound fault originates almost all of them (background noise of one request).
 3. A spike outside the fault's minutes, larger than the fault's own peak, does not drop a question without an entry
    operation; or a small blip above baseline does.
 4. The fault's own minutes (with a minute either side) count as unexplained.
 5. Minutes with no errors are left out of the baseline, so the baseline is the median of the busy minutes.
"""

from __future__ import annotations

from dataclasses import dataclass

from otb.tasks.instantiate import mixed_origins, unexplained_spike
from otb.timebase import NS, EventNs, Window

M = 60 * NS
SCOPE = Window(EventNs(0), EventNs(20 * M))
FAULT = {
    "kind": "fault",
    "instance": "i1",
    "root_service": "payment",
    "symptom_window": Window(EventNs(5 * M), EventNs(7 * M)),
}
SCHEDULED = {"service": {"source": "schedule"}}


@dataclass
class Rows:
    rows: list[dict]

    def as_dicts(self) -> list[dict]:
        return self.rows


class Ctx:
    def __init__(self, **measures: list[dict]) -> None:
        self.measures = measures

    def run(self, name: str, params: dict, instance: str) -> Rows:
        assert instance == "i1"
        return Rows(self.measures[name])


def origins(**n: int) -> list[dict]:
    total = sum(n.values())
    return [{"service": s, "operation": "op", "n": k, "failures": total, "share": k / total} for s, k in n.items()]


def test_mixed_origins() -> None:
    env = {"fault": FAULT, "entry_svc": "frontend", "entry_op": "POST /api/checkout"}
    why = mixed_origins(env, SCOPE, Ctx(deepest_error_span=origins(payment=20, checkout=11, frontend=3)))  # type: ignore[arg-type]
    assert why == (
        "no dominant origin: the bound fault originates 59% of 34 failing requests"
        " (payment 20, checkout 11, frontend 3)"
    )
    assert mixed_origins(env, SCOPE, Ctx(deepest_error_span=origins(payment=40, frontend=1))) is None  # type: ignore[arg-type]
    assert mixed_origins({"fault": FAULT}, SCOPE, Ctx()) is None  # type: ignore[arg-type]


def per_minute(**at: int) -> list[dict]:
    return [{"minute_ns": int(k[1:]) * M, "n": v} for k, v in at.items()]


def test_unexplained_spike() -> None:
    env = {"fault": FAULT}
    quiet = {f"m{i}": 2 for i in range(20) if i not in (4, 5, 6, 7, 15)}
    big = Ctx(error_spans_per_minute=per_minute(**quiet, m5=30, m6=25, m15=40))
    why = unexplained_spike(env, SCHEDULED, SCOPE, big)  # type: ignore[arg-type]
    assert (
        why == "unexplained error spike at 1970-01-01T00:15Z: 40 error spans/min, baseline 2, the fault's own peak 30"
    )
    blip = Ctx(error_spans_per_minute=per_minute(**quiet, m5=30, m15=6))
    assert unexplained_spike(env, SCHEDULED, SCOPE, blip) is None  # type: ignore[arg-type]
    near = Ctx(error_spans_per_minute=per_minute(**quiet, m4=40, m7=40, m5=30))
    assert unexplained_spike(env, SCHEDULED, SCOPE, near) is None, "the minute either side is the fault's"  # type: ignore[arg-type]
    sparse = Ctx(error_spans_per_minute=per_minute(m5=30, m15=20))
    assert "baseline 0" in (unexplained_spike(env, SCHEDULED, SCOPE, sparse) or ""), "empty minutes count as zero"  # type: ignore[arg-type]
    assert unexplained_spike(env | {"entry_op": "x"}, SCHEDULED, SCOPE, big) is None  # type: ignore[arg-type]
