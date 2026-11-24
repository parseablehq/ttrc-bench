"""Two guards of `otb tasks instantiate`, tested in isolation: which schedule entries a negative-control twin may
bind (by the schedule_check status that F1's decoy and quiet-window confirmation fills in), and whether an onset
can be read off the prompt.

Ways they could be wrong, written before the code:
 1. A decoy whose effect schedule_check did not observe, or a window it found loud, becomes a control.
 2. An entry schedule_check left `unchecked` (a decoy type with no effect rule), or has no check for, becomes a
    control.
 3. A confirmed decoy or a quiet window is refused, so no corpus has controls.
 4. A report time within an onset's tolerance passes unnoticed when the onset sits inside an object list's records.
"""

import dataclasses
import json
from pathlib import Path

from otb.scenario import RealisedSchedule
from otb.tasks.instantiate import entry_value, prompt_leak
from otb.tasks.suite import Truth, parse_instance
from otb.tasks.template import TEMPLATES_DIR, load_vocab, parse_template
from otb.timebase import Shift

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
FIXTURE = FIXTURES / "suite-mini"


def test_only_confirmed_quiet_windows_may_host_a_twin() -> None:
    (fault,) = RealisedSchedule.from_yaml((FIXTURES / "corpus-mini/schedule.yaml").read_bytes()).entries
    decoy = dataclasses.replace(fault, slot_id="d1", kind="decoy", decoy_type="traffic_gap")
    quiet = dataclasses.replace(fault, slot_id="q1", kind="quiet")

    def refusal(e: object, **check: object) -> str | None:
        return entry_value(e, {"kind": e.kind, "quiet": True, "loud_services": [], **check})["twin_refusal"]

    assert refusal(decoy, status="realised") is None
    assert refusal(quiet, status="realised") is None
    assert refusal(decoy, status="no_effect") == "decoy effect not seen: no_effect"
    assert refusal(decoy, status="unchecked") == "decoy effect not seen: unchecked"
    assert refusal(decoy, status="failed") == "decoy effect not seen: failed"
    assert (
        refusal(quiet, status="realised", quiet=False, loud_services=["checkout", "payment"])
        == "not quiet: checkout, payment errors above the baseline band"
    )
    assert refusal(fault, status="no_symptom") == "a fault window"
    assert entry_value(decoy, None)["twin_refusal"] == "schedule_check missing"


def test_report_time_inside_an_onset_tolerance_is_a_leak() -> None:
    """The hand-written suite-mini L5 instance pages 20 s before its first cause's onset (within the 120 s
    tolerance), and 870 s before the second's."""
    t = parse_template(TEMPLATES_DIR / "l5-two-concurrent.yaml", load_vocab())
    lines = (FIXTURE / "instances.jsonl").read_text().splitlines()
    raw = next(json.loads(x) for x in lines if '"fx-l5-two-concurrent--c1"' in x)
    truth = next(json.loads(x) for x in (FIXTURE / "truth.jsonl").read_text().splitlines() if raw["instance_id"] in x)
    want = Truth(raw["instance_id"], truth["answer"], {}).wall_answer(parse_instance(raw), Shift(0))
    assert prompt_leak(t, raw, want, None) == "read_window_off_prompt answers causes.onset_at of 1 record(s)"
