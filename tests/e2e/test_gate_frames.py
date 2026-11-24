"""A rejected, missing or in-doubt frame excludes only the instances whose window holds its records, through the
real path: a feeder replaying corpus-mini against a stand-in collector that rejects one frame, the ledger perturbed
by hand for the other two kinds, the ingest report, the judge and dispatch's exclusion.

Ways it could be wrong, written before the code:
 1. One bad frame among the signal's frames still fails `ingest.ledger:<signal>` whole, so every instance on the
    signal is excluded.
 2. The frame's check is windowed to something other than its records' event span (its due or received time), so
    an instance whose window holds the lost records runs, or one next to them is excluded.
 3. A frame of one signal excludes an instance on another signal.
 4. A missing frame (no outcome) or an in-doubt one (resent, may be stored twice) is not failed at all, or is failed
    whole when the rejected one is localised.
 5. The frame's windowed check counts as covering a footprint, so an instance no stored-data check reaches runs.
 6. A report whose span list does not account for every frame of a kind (the cap, or a report written before the
    spans existed) localises what it has instead of failing the signal whole, as the gate did before.
 7. The clean case changes: tests/e2e/test_gate_rejudge.py holds the committed gate.json byte-identical.

Evidence: tests/e2e/artifacts/gate_frames.json.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from conftest import write_artifact
from fake_collector import serve, target
from synthetic import NS

from otb.archive import Archive
from otb.footprint import Footprint, Region, covered
from otb.gate import Plan, Point, judge_stack
from otb.ingest import frame_name, read_ledger, replay_body, run_session, selected_frames, stack_report, start_session
from otb.run.dispatch import exclusion
from otb.stacks.contract import CountLogs, CountSpans, Exact, IngestCounters, MetricNames
from otb.tasks.suite import Instance, parse_instance
from otb.timebase import EventNs, Window

REPO = Path(__file__).resolve().parents[2]
MINI = REPO / "tests/fixtures/corpus-mini"
WINDOW = Window(EventNs(1790237610 * NS), EventNs(1790237620 * NS))
SCOPE = Window(EventNs(WINDOW.start - 60 * NS), EventNs(WINDOW.end + 60 * NS))  # the passing checks cover this much


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def instance(signal: str, window: Window) -> Instance:
    fp = Footprint((Region(signal, None, window),))  # type: ignore[arg-type]
    return parse_instance(
        {
            "instance_id": f"i-{signal}-{window.start}",
            "template_id": "t",
            "level": 1,
            "k_design": 1,
            "s": 1,
            "signals": [signal],
            "statement": "q",
            "scope": [SCOPE.start, SCOPE.end],
            "fields": {"n": {"type": "integer"}},
            "footprint": fp.to_json(),
        }
    )


def verdict(ingest: dict) -> dict:
    points = tuple(Point.of(c, SCOPE) for c in (CountLogs(None, False), CountSpans(None, False), MetricNames()))
    plan = Plan(0, ingest["session"], "c", {}, points)
    return judge_stack(plan, {p.id: 1 for p in points}, {p.id: Exact(1) for p in points}, ingest, {})


def test_one_bad_frame_excludes_only_the_instances_on_its_records(tmp_path: Path, artifacts: Path) -> None:
    archive = Archive(MINI)
    session, _ = start_session(MINI, ("s",), "replay", window=WINDOW, lead_s=2, repo=tmp_path)
    selected = sorted(selected_frames(archive, WINDOW), key=lambda x: (x[1].max_event_ns, x[0]))
    assert len(selected) >= 4, "the window holds a frame to reject, one to lose, one to doubt and one left alone"
    (rejected, e_rej), (missing, _), (doubted, _) = selected[0], selected[1], selected[2]
    sha = hashlib.sha256(replay_body(archive.read(rejected, e_rej), e_rej, session.shift)).hexdigest()
    port = free_port()
    server = serve(port, tmp_path / "s.log", {sha}, 0, 0.0)
    run_session(session, [target("s", f"http://127.0.0.1:{port}")], tmp_path, log=lambda m: None)
    server.shutdown()

    ledger = session.ledger_path("s", tmp_path)
    as_list = lambda r: [r.signal, r.stream, r.seq, r.index]  # noqa: E731
    kept = [x for x in read_ledger(ledger) if x.get("f") != as_list(missing)]  # neither intent nor outcome: lost
    kept += [{"ev": "intent", "f": as_list(doubted)}, {"ev": "start", "at_ns": 0, "in_doubt": 1}]
    ledger.write_text("".join(json.dumps(x) + "\n" for x in kept))
    report = stack_report(archive, session, "s", target("s", "").max_late_s, ledger)
    ingest = json.loads(json.dumps(report.to_json()))
    assert (report.finished, report.complete, [r["frame"] for r in report.rejected], report.in_doubt) == (
        False,
        False,
        [frame_name(rejected)],
        [frame_name(doubted)],
    )

    spans = {ref: (e.min_event_ns, e.max_event_ns + 1) for ref, e in selected}  # half-open: the newest record is in
    v = verdict(ingest)
    frame_checks = {
        k: c
        for k, c in v["checks"].items()
        if k.split(":")[0] in ("ingest.rejected", "ingest.missing", "ingest.in_doubt")
    }
    assert {k: (c["signal"], tuple(c["region"]["window"]), c["status"]) for k, c in frame_checks.items()} == {
        f"ingest.rejected:{frame_name(rejected)}": (rejected.signal, spans[rejected], "fail"),
        f"ingest.missing:{frame_name(missing)}": (missing.signal, spans[missing], "fail"),
        f"ingest.in_doubt:{frame_name(doubted)}": (doubted.signal, spans[doubted], "fail"),
    }
    assert {
        s: c["status"] for s, c in ((s, v["checks"][f"ingest.ledger:{s}"]) for s in ("traces", "logs", "metrics"))
    } == {
        "traces": "pass",
        "logs": "pass",
        "metrics": "pass",
    }, "the ledger check no longer fails a whole signal for a frame it localised"
    assert v["ingest_reasons"] == []
    assert v["checks"][f"ingest.rejected:{frame_name(rejected)}"]["got"] == "HTTP 400"

    gate = {"stacks": {"s": v}}
    table = {}
    for name, ref in (("rejected", rejected), ("missing", missing), ("in_doubt", doubted)):
        a, b = spans[ref]
        on = instance(ref.signal, Window(EventNs(a), EventNs(b)))
        hits = sorted(
            k
            for k, c in frame_checks.items()
            if c["signal"] == ref.signal and max(c["region"]["window"][0], a) < min(c["region"]["window"][1], b)
        )
        table[name] = {
            "frame": frame_name(ref),
            "window": [a, b],
            "on_its_records": exclusion(gate, "s", on),
            "before_its_records": exclusion(gate, "s", instance(ref.signal, Window(EventNs(a - 10 * NS), EventNs(a)))),
            "other_signal": exclusion(
                gate,
                "s",
                instance(
                    next(x for x in ("logs", "traces", "metrics") if x != ref.signal), Window(EventNs(a), EventNs(b))
                ),
            ),
        }
        assert table[name]["on_its_records"] == "gate on s: " + ", ".join(f"{k} fail" for k in hits)
        assert f"ingest.{name}:{frame_name(ref)}" in hits
        assert table[name]["before_its_records"] is None, name
        assert covered(frame_checks, on.footprint) is False, "a frame check vouches for no stored data"
    other = [t["other_signal"] for t in table.values()]
    assert all(x is None or x.startswith("gate on s: ingest.") for x in other)
    assert any(x is None for x in other), "a frame excludes nothing on another signal"

    capped = json.loads(json.dumps(ingest))
    capped["frame_spans"]["rejected"][rejected.signal] = []
    whole = verdict(capped)["checks"][f"ingest.ledger:{rejected.signal}"]
    assert (whole["status"], whole["reason"]) == ("fail", f"1 frames rejected: {frame_name(rejected)}")
    assert not any(k.startswith("ingest.rejected:") for k in verdict(capped)["checks"])

    legacy = {k: x for k, x in ingest.items() if k != "frame_spans"}
    old = verdict(legacy)
    assert not any(k.split(":")[0] in ("ingest.rejected", "ingest.missing", "ingest.in_doubt") for k in old["checks"])
    assert old["checks"][f"ingest.ledger:{missing.signal}"]["status"] == "fail"
    assert old["ingest_reasons"][-1] == f"1 frames in doubt (resent after a feeder died): ['{frame_name(doubted)}']"
    both = json.loads(json.dumps(legacy))
    both["signals"][rejected.signal]["frames_missing"] = 1
    assert verdict(both)["checks"][f"ingest.ledger:{rejected.signal}"]["reason"] == (
        f"1 frames without an outcome; 1 frames rejected: {frame_name(rejected)}"
    ), "the order the gate wrote before frames were localised, so old evidence rejudges byte for byte"

    write_artifact(
        artifacts / "gate_frames.json",
        {
            "window": [WINDOW.start, WINDOW.end],
            "frames_selected": len(selected),
            "ledger": {s: v["checks"][f"ingest.ledger:{s}"]["reason"] for s in ("traces", "logs", "metrics")},
            "frame_checks": {k: [c["signal"], c["region"]["window"], c["got"]] for k, c in frame_checks.items()},
            "exclusions": table,
            "capped_ledger": [whole["status"], whole["reason"]],
            "legacy_reasons": old["ingest_reasons"],
        },
    )


def test_a_rejected_frame_is_accounted_in_the_collector_counters(tmp_path: Path) -> None:
    """A collector counts a record the backend rejected for good (4xx) as it counts one answered 5xx: once refused,
    once send-failed. The frame's own windowed check fails it, so the unwindowed counter check must expect those
    counts and fail only on refusals the ledger never saw.

    Ways it could be wrong, written before the code:
     1. The counter check expects only the 5xx-then-resent records, so one rejected frame fails the whole signal
        and excludes every instance on it (gke-r2-24h: B1's 9 frames, 2929 records, counted 5858).
     2. The check absorbs refusals the ledger never saw: a count past twice the rejected and resent records passes.
     3. A report written before it named rejected records judges differently, so old gate.json stops rejudging.
    """
    archive = Archive(MINI)
    session, _ = start_session(MINI, ("s",), "replay", window=WINDOW, lead_s=2, repo=tmp_path)
    selected = sorted(selected_frames(archive, WINDOW), key=lambda x: (x[1].max_event_ns, x[0]))
    rejected, e_rej = selected[0]
    sha = hashlib.sha256(replay_body(archive.read(rejected, e_rej), e_rej, session.shift)).hexdigest()
    port = free_port()
    server = serve(port, tmp_path / "s.log", {sha}, 0, 0.0)
    run_session(session, [target("s", f"http://127.0.0.1:{port}")], tmp_path, log=lambda m: None)
    server.shutdown()
    report = stack_report(archive, session, "s", target("s", "").max_late_s, session.ledger_path("s", tmp_path))
    ingest = json.loads(json.dumps(report.to_json()))
    sig = rejected.signal
    assert ingest["signals"][sig]["rejected_records"] == e_rej.records > 0
    assert all(ingest["signals"][s]["rejected_records"] == 0 for s in ("traces", "logs", "metrics") if s != sig)

    point = Point.of(IngestCounters(), None)
    plan = Plan(0, ingest["session"], "c", {}, (point,))

    def counters(report_json: dict, counted: int) -> dict:
        seen = {s: 0 for s in ("traces", "logs", "metrics")} | {sig: counted}
        return judge_stack(plan, {point.id: 0}, {point.id: Exact(seen)}, report_json, {})["checks"][f"{point.id}:{sig}"]

    exact = counters(ingest, 2 * e_rej.records)
    assert (exact["status"], exact["expected"], exact["got"], exact["region"]["window"]) == (
        "pass",
        2 * e_rej.records,
        2 * e_rej.records,
        None,
    )
    extra = counters(ingest, 2 * e_rej.records + 2)
    assert (extra["status"], extra["reason"]) == (
        "fail",
        f"{2 * e_rej.records + 2} refused or failed records counted since the session started; the ledger accounts"
        f" for {2 * e_rej.records} (0 records answered 5xx, then resent, {e_rej.records} rejected)",
    )

    legacy = json.loads(json.dumps(ingest))
    for s in legacy["signals"].values():
        del s["rejected_records"]
    old = counters(legacy, 2 * e_rej.records)
    assert (old["status"], old["expected"], old["reason"]) == (
        "fail",
        0,
        f"{2 * e_rej.records} refused or failed records counted since the session started; the ledger accounts for 0"
        " (0 records answered 5xx, then resent)",
    ), "a report from before rejected_records judges as the gate judged it then"
