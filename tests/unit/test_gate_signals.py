"""A gate signal's status and the exclusion it drives, tested in isolation because every cell's exclusion reads them.

Ways it could be wrong, written before the code:
 1. A signal whose checks of stored data are all unverifiable reports pass, because only `fail` is looked for.
 2. A passing ledger or counter check (no window, reads no stored data) makes such a signal pass.
 3. One unverifiable check among passing ones makes the whole signal unverifiable, so a single unsupported probe
    excludes every instance on that signal.
 4. An unverifiable signal does not exclude an instance whose footprint reads it, or excludes one that does not.
"""

from __future__ import annotations

from otb.footprint import Footprint, Region
from otb.gate import signal_status
from otb.run.dispatch import exclusion
from otb.tasks.suite import Instance, parse_instance
from otb.timebase import EventNs, Window

W = [1_000, 2_000]


def check(status: str, signal: str = "traces", window: list[int] | None = W) -> dict:
    return {"status": status, "region": {"signal": signal, "services": None, "window": window, "metrics": None}}


def test_signal_status() -> None:
    assert signal_status([check("unverifiable"), check("unverifiable")]) == "unverifiable"
    assert signal_status([check("pass", window=None), check("unverifiable")]) == "unverifiable"
    assert signal_status([check("pass"), check("unverifiable")]) == "pass"
    assert signal_status([check("pass"), check("fail", window=None)]) == "fail"


def instance(signals: list[str]) -> Instance:
    fp = Footprint(tuple(Region(s, None, Window(EventNs(1_000), EventNs(2_000))) for s in signals))  # type: ignore[arg-type]
    return parse_instance(
        {
            "instance_id": "i",
            "template_id": "t",
            "level": 1,
            "k_design": 1,
            "s": 1,
            "signals": signals,
            "statement": "q",
            "scope": W,
            "fields": {"n": {"type": "integer"}},
            "footprint": fp.to_json(),
        }
    )


def test_unverifiable_signal_excludes_only_footprints_that_read_it() -> None:
    checks = {"t": check("unverifiable"), "l": check("pass", "logs"), "ledger": check("pass", window=None)}
    gate = {"stacks": {"s": {"signals": {"traces": "unverifiable", "logs": "pass"}, "checks": checks}}}
    assert (
        exclusion(gate, "s", instance(["traces"])) == "gate on s: traces unverifiable (no check of stored data passed)"
    )
    assert exclusion(gate, "s", instance(["logs"])) is None
