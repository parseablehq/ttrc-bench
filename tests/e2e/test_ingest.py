"""Feeders against stand-in collectors (tests/e2e/fake_collector.py), replaying 20 s of corpus-mini at 1x.

Ways a feeder can fail, each asserted below:
- a frame is never sent, sent twice in one run, or sent with timestamps not moved by the shift, including the
  envoy access-log time inside a log body;
- a frame is sent before it is due, or the shift is not whole minutes or puts the first frame before lead_s;
- a 4xx stops the feeder, or is retried forever, or its body is lost (amendment 4);
- a 5xx or a 429 loses the frame instead of retrying it;
- a transport error after the request went out is retried without marking the frame in doubt;
- a second feeder opens a ledger another feeder holds;
- after SIGKILL, a rerun picks a new session or shift, resends acked frames, or forgets which frames were in
  flight (in doubt), or a torn ledger line breaks the rerun;
- the report claims completeness that the collector's own log contradicts.

Each stack has its own feeder process (U10). Ways that split can fail, each asserted below:
- a feeder process outlives a SIGKILLed ingest, keeps writing its ledger, or holds the ledger lock against the
  rerun;
- SIGTERM to the ingest is not forwarded, so a feeder keeps sending; or a feeder stops without finishing its
  in-flight frames, leaving them in doubt; or a stopped session is marked done;
- two stacks are fed from one process, or from the ingest process itself;
- a busy ledger on one stack lets the other stacks start sending before the session is refused.

A follow session outlives its capture and its collectors. Ways that can fail, each asserted below:
- the capture seals while no ingest runs, and the rerun starts a new session that resends every frame instead of
  resuming the one that fed the unsealed archive, or the resumed session keeps "unsealed" as its corpus id;
- a collector restarts inside the session, its refusal counters restart from zero, and the gate's baseline (the
  first reading) makes the counted refusals since the session started wrong or negative; a reading taken while
  the collector is down counts as zero; no reading is taken after the feeders exit.

Evidence: tests/e2e/artifacts/ingest_fake.json.
"""

import fcntl
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from conftest import write_artifact
from fake_collector import serve, target

from otb.archive import Archive, FrameRef
from otb.ingest import (
    Ledger,
    LedgerBusy,
    Session,
    feeder_stats_path,
    frame_name,
    read_ledger,
    replay_body,
    run_session,
    selected_frames,
    stack_report,
    start_session,
)
from otb.timebase import NS, EventNs, Window

REPO = Path(__file__).resolve().parents[2]
MINI = REPO / "tests/fixtures/corpus-mini"
WINDOW = Window(EventNs(1790237610 * NS), EventNs(1790237630 * NS))
LEAD_S = 2


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def received(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def expected_shas(archive: Archive, session: Session) -> dict[str, str]:
    """frame name -> sha of the shifted body the collector must receive."""
    return {
        frame_name(r): hashlib.sha256(replay_body(archive.read(r, e), e, session.shift)).hexdigest()
        for r, e in selected_frames(archive, session.window)
    }


def update_artifact(artifacts: Path, key: str, value: dict) -> None:
    path = artifacts / "ingest_fake.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    data[key] = value
    write_artifact(path, data, sort_keys=True)


def test_replay_at_1x_with_rejection_and_retries(tmp_path: Path, artifacts: Path) -> None:
    archive = Archive(MINI)
    selected = list(selected_frames(archive, WINDOW))
    t0 = time.time_ns()
    session, resumed = start_session(
        MINI, ("good", "flaky", "strict"), "replay", window=WINDOW, lead_s=LEAD_S, repo=tmp_path
    )
    want = expected_shas(archive, session)
    bad_frame = sorted(selected, key=lambda x: x[1].max_event_ns)[2][0]
    bad_sha = want[frame_name(bad_frame)]
    logs = {n: tmp_path / f"{n}.log" for n in ("good", "flaky", "strict")}
    ports = {n: free_port() for n in logs}
    servers = [
        serve(ports["good"], logs["good"], set(), 0, 0.0),
        serve(ports["flaky"], logs["flaky"], set(), 3, 0.0),
        serve(ports["strict"], logs["strict"], {bad_sha}, 0, 0.0),
    ]
    targets = [target(n, f"http://127.0.0.1:{p}") for n, p in ports.items()]
    reports = run_session(session, targets, tmp_path, log=lambda m: None)
    for s in servers:
        s.shutdown()

    first_due = min(e.max_event_ns for _, e in selected) + session.shift_s * NS
    ledger = read_ledger(session.ledger_path("good", tmp_path))
    outcomes = [x for x in ledger if x["ev"] == "outcome"]
    got = {n: Counter(r["sha"] for r in received(log) if r["code"] == 200) for n, log in logs.items()}
    result = {
        "frames_selected": len(selected),
        "shift_whole_minutes": session.shift_s % 60 == 0,
        "first_due_after_start_s": round((first_due - t0) / NS, 1),
        "sent_before_due": sum(1 for o in outcomes if o["sent_ns"] < o["due_ns"]),
        "good_bodies_equal_shifted": got["good"] == Counter(want.values()),
        "flaky_bodies_equal_shifted": got["flaky"] == Counter(want.values()),
        "strict_bodies_equal_shifted_minus_rejected": got["strict"]
        == Counter(v for v in want.values() if v != bad_sha),
        "reports": {
            n: {"finished": r.finished, "complete": r.complete, "rejected": r.rejected} for n, r in reports.items()
        },
        "flaky_retries": sum(v.retries for v in reports["flaky"].signals.values()),
        "good_late_max_s": max(v.late_max_s or 0 for v in reports["good"].signals.values()),
        "done_marker": (session.dir(tmp_path) / "ingest" / "done.json").exists(),
        "time_strings_left": {n: sum(v.time_strings_left or 0 for v in r.signals.values()) for n, r in reports.items()},
        "recording_date_in_raw_bodies": sum(archive.read(r, e).count(b"[2026-09-24T") for r, e in selected),
        "recording_date_in_sent_bodies": sum(
            replay_body(archive.read(r, e), e, session.shift).count(b"[2026-09-24T") for r, e in selected
        ),
        "shifted_date_in_sent_bodies": sum(
            replay_body(archive.read(r, e), e, session.shift).count(b"[" + shifted_day(session.shift_s) + b"T")
            for r, e in selected
        ),
    }
    update_artifact(
        artifacts,
        "replay",
        {k: v for k, v in result.items() if k not in ("first_due_after_start_s", "good_late_max_s")}
        | {"rejected_frame": frame_name(bad_frame)},
    )

    assert not resumed
    assert result["frames_selected"] > 10
    assert result["shift_whole_minutes"]
    assert LEAD_S <= result["first_due_after_start_s"] <= LEAD_S + 61
    assert result["sent_before_due"] == 0
    assert result["good_bodies_equal_shifted"] and result["flaky_bodies_equal_shifted"]
    assert result["strict_bodies_equal_shifted_minus_rejected"]
    assert result["reports"]["good"] == {"finished": True, "complete": True, "rejected": []}
    assert result["reports"]["flaky"] == {"finished": True, "complete": True, "rejected": []}
    assert result["reports"]["strict"] == {
        "finished": True,
        "complete": False,
        "rejected": [{"frame": frame_name(bad_frame), "http": 400, "body": '{"error":"fake rejection"}'}],
    }
    assert result["flaky_retries"] == 3
    assert result["time_strings_left"] == {"good": 0, "flaky": 0, "strict": 0}
    assert result["recording_date_in_sent_bodies"] == 0
    assert result["shifted_date_in_sent_bodies"] == result["recording_date_in_raw_bodies"] > 0
    assert result["good_late_max_s"] < 2.0
    assert result["done_marker"]


def shifted_day(shift_s: int) -> bytes:
    """The day of 2026-09-24T08:10Z, the slice's start, moved by the shift, as the replay must write it."""
    return datetime.fromtimestamp(1790237400 + shift_s, UTC).strftime("%Y-%m-%d").encode()


def test_429_is_retried_and_a_dropped_answer_leaves_the_frame_in_doubt(tmp_path: Path, artifacts: Path) -> None:
    archive = Archive(MINI)
    selected = sorted(selected_frames(archive, WINDOW), key=lambda x: x[1].max_event_ns)
    session, _ = start_session(MINI, ("busy", "dropper"), "replay", window=WINDOW, lead_s=LEAD_S, repo=tmp_path)
    ports = {"busy": free_port(), "dropper": free_port()}
    servers = [
        serve(ports["busy"], tmp_path / "busy.log", set(), 2, 0.0, fail_code=429),
        serve(ports["dropper"], tmp_path / "dropper.log", set(), 0, 0.0, drop_first=1),
    ]
    reports = run_session(
        session, [target(n, f"http://127.0.0.1:{p}") for n, p in ports.items()], tmp_path, log=lambda m: None
    )
    for s in servers:
        s.shutdown()
    retries = [x for x in read_ledger(session.ledger_path("dropper", tmp_path)) if x["ev"] == "retry"]
    result = {
        "busy": {
            "complete": reports["busy"].complete,
            "retries": sum(v.retries for v in reports["busy"].signals.values()),
            "in_doubt": reports["busy"].in_doubt,
        },
        "dropper": {
            "complete": reports["dropper"].complete,
            "retries": len(retries),
            "in_doubt": reports["dropper"].in_doubt,
        },
        "dropper_retry_error": retries[0]["error"].split(":")[0],
    }
    update_artifact(artifacts, "retry_and_doubt", result)
    assert result == {
        "busy": {"complete": True, "retries": 2, "in_doubt": []},
        "dropper": {"complete": True, "retries": 1, "in_doubt": [frame_name(selected[0][0])]},
        "dropper_retry_error": "RemoteDisconnected",
    }


def test_a_second_feeder_on_the_same_ledger_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "b1.ledger.jsonl"
    first = Ledger(path)
    try:
        Ledger(path)
    except LedgerBusy as e:
        refused = str(e)
    else:
        refused = ""
    first.close()
    third = Ledger(path)
    third.close()
    assert refused == f"{path}: another feeder holds this ledger; refusing to feed the same session"


def drive(
    repo: Path, *ports: int, names: tuple[str, ...] = ("slow",), corpus: Path = MINI, mode: str = "replay"
) -> subprocess.Popen:
    """A feeder process: `drive` replays WINDOW of MINI, `follow` feeds a live corpus from its start."""
    targets = [f"{n}=http://127.0.0.1:{p}" for n, p in zip(names, ports, strict=True)]
    if mode == "follow":
        args = ["follow", str(repo), str(corpus), *targets]
    else:
        args = ["drive", str(repo), str(corpus), f"{WINDOW.start}/{WINDOW.end}", str(LEAD_S), *targets]
    return subprocess.Popen(
        [sys.executable, str(REPO / "tests/e2e/fake_collector.py"), *args],
        stdout=subprocess.PIPE,
        text=True,
        env=os.environ | {"PYTHONPATH": str(REPO)},
    )


def seconds_until_free(ledger: Path, limit_s: float = 30.0) -> float:
    """How long until no process holds the ledger's flock: a feeder process whose ingest died must exit."""
    t0 = time.monotonic()
    with ledger.open("ab") as f:
        while time.monotonic() - t0 < limit_s:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                time.sleep(0.02)
                continue
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            break
    return time.monotonic() - t0


def test_sigkill_and_resume(tmp_path: Path, artifacts: Path) -> None:
    port = free_port()
    log = tmp_path / "slow.log"
    server = serve(port, log, set(), 0, 1.5)
    first = drive(tmp_path, port)
    started = json.loads(first.stdout.readline())
    ledger_path = Session.load(tmp_path / "state/sessions" / started["session"] / "session.json").ledger_path(
        "slow", tmp_path
    )
    while sum(1 for x in read_ledger(ledger_path) if x["ev"] == "outcome") < 5:
        assert first.poll() is None, "the feeder finished before it could be killed"
        time.sleep(0.05)
    first.send_signal(signal.SIGKILL)
    first.wait()
    orphan_s = seconds_until_free(ledger_path)
    ledger_at_free = ledger_path.read_bytes()
    time.sleep(2.0)
    assert ledger_path.read_bytes() == ledger_at_free, "a feeder process kept writing after its ingest was killed"
    assert orphan_s < 5.0, f"the feeder process outlived its killed ingest by {orphan_s:.1f} s"
    lines = read_ledger(ledger_path)
    acked_before = {tuple(x["f"]) for x in lines if x["ev"] == "outcome"}
    open_before = {tuple(x["f"]) for x in lines if x["ev"] == "intent"} - acked_before
    with ledger_path.open("ab") as f:
        f.write(b'{"ev":"outcome","f":["traces"')  # a torn line, as a kill mid-write leaves

    second = drive(tmp_path, port)
    resumed = json.loads(second.stdout.readline())
    second.wait(timeout=300)
    server.shutdown()

    session = Session.load(tmp_path / "state/sessions" / started["session"] / "session.json")
    archive = Archive(MINI)
    report = stack_report(archive, session, "slow", {"traces": None, "logs": None, "metrics": None}, ledger_path)
    want = expected_shas(archive, session)
    arrivals = Counter(r["sha"] for r in received(log))
    twice = sorted(name for name, sha in want.items() if arrivals[sha] > 1)
    result = {
        "killed_after_outcomes": len(acked_before),
        "in_flight_at_kill": sorted(frame_name(FrameRef(*f)) for f in open_before),
        "second_run_resumed_same_session": resumed["resumed"] and resumed["session"] == started["session"],
        "same_shift": resumed["shift_s"] == started["shift_s"],
        "report_finished": report.finished,
        "report_in_doubt": report.in_doubt,
        "every_frame_received": all(arrivals[sha] >= 1 for sha in want.values()),
        "received_twice": twice,
        "acked_frames_resent": sum(
            1 for x in read_ledger(ledger_path) if x["ev"] == "intent" and tuple(x["f"]) in acked_before
        )
        - len(acked_before),
    }
    timing = ("killed_after_outcomes", "in_flight_at_kill", "report_in_doubt", "received_twice")
    update_artifact(
        artifacts,
        "kill_resume",
        {k: v for k, v in result.items() if k not in timing}
        | {
            "in_doubt_equals_in_flight": result["report_in_doubt"] == result["in_flight_at_kill"],
            "received_twice_only_in_flight": set(result["received_twice"]) <= set(result["in_flight_at_kill"]),
        },
    )

    assert second.returncode == 0
    assert result["in_flight_at_kill"], "the kill must land with frames in flight"
    assert result["second_run_resumed_same_session"] and result["same_shift"]
    assert result["report_finished"]
    assert result["report_in_doubt"] == result["in_flight_at_kill"]
    assert result["every_frame_received"]
    assert set(result["received_twice"]) <= set(result["in_flight_at_kill"])
    assert result["acked_frames_resent"] == 0


def outcomes(path: Path) -> int:
    return sum(1 for x in read_ledger(path) if x["ev"] == "outcome")


def test_sigterm_is_forwarded_to_one_process_per_stack(tmp_path: Path, artifacts: Path) -> None:
    names = ("a", "b")
    ports = tuple(free_port() for _ in names)
    logs = {n: tmp_path / f"{n}.log" for n in names}
    servers = [serve(p, logs[n], set(), 0, 1.0) for n, p in zip(names, ports, strict=True)]
    first = drive(tmp_path, *ports, names=names)
    started = json.loads(first.stdout.readline())
    session = Session.load(tmp_path / "state/sessions" / started["session"] / "session.json")
    ledgers = {n: session.ledger_path(n, tmp_path) for n in names}
    while min(outcomes(p) for p in ledgers.values()) < 3:
        assert first.poll() is None, "the session finished before it could be stopped"
        time.sleep(0.05)
    first.send_signal(signal.SIGTERM)
    first.wait(timeout=60)
    stats = {
        n: [json.loads(x) for x in feeder_stats_path(session, n, tmp_path).read_text().splitlines()] for n in names
    }
    open_after_stop = {
        n: sorted(
            {tuple(x["f"]) for x in read_ledger(p) if x["ev"] == "intent"}
            - {tuple(x["f"]) for x in read_ledger(p) if x["ev"] == "outcome"}
        )
        for n, p in ledgers.items()
    }
    stopped_done = (session.dir(tmp_path) / "ingest" / "done.json").exists()

    second = drive(tmp_path, *ports, names=names)
    resumed = json.loads(second.stdout.readline())
    second.wait(timeout=300)
    for s in servers:
        s.shutdown()
    archive = Archive(MINI)
    want = expected_shas(archive, session)
    arrivals = {n: Counter(r["sha"] for r in received(logs[n])) for n in names}
    pids = [line["pid"] for n in names for line in stats[n]]
    result = {
        "stopped_exit_code": first.returncode,
        "feeder_runs_after_stop": {n: len(v) for n, v in stats.items()},
        "every_feeder_stopped": all(line["stopped"] for v in stats.values() for line in v),
        "one_process_per_stack": len(set(pids)) == len(names) and first.pid not in pids,
        "open_intents_after_stop": open_after_stop,
        "done_marker_after_stop": stopped_done,
        "rerun_resumed": resumed["resumed"] and resumed["session"] == started["session"],
        "rerun_exit_code": second.returncode,
        "every_frame_received_once": {
            n: all(arrivals[n][sha] == 1 for sha in want.values()) and sum(arrivals[n].values()) == len(want)
            for n in names
        },
        "done_marker_after_rerun": (session.dir(tmp_path) / "ingest" / "done.json").exists(),
    }
    update_artifact(artifacts, "sigterm_processes", result)
    assert result == {
        "stopped_exit_code": 0,
        "feeder_runs_after_stop": {"a": 1, "b": 1},
        "every_feeder_stopped": True,
        "one_process_per_stack": True,
        "open_intents_after_stop": {"a": [], "b": []},
        "done_marker_after_stop": False,
        "rerun_resumed": True,
        "rerun_exit_code": 0,
        "every_frame_received_once": {"a": True, "b": True},
        "done_marker_after_rerun": True,
    }


def test_a_busy_ledger_refuses_the_session_before_any_stack_sends(tmp_path: Path) -> None:
    session, _ = start_session(MINI, ("free", "held"), "replay", window=WINDOW, lead_s=0, repo=tmp_path)
    ports = {"free": free_port(), "held": free_port()}
    servers = [serve(p, tmp_path / f"{n}.log", set(), 0, 0.0) for n, p in ports.items()]
    holder = Ledger(session.ledger_path("held", tmp_path))
    try:
        run_session(session, [target(n, f"http://127.0.0.1:{p}") for n, p in ports.items()], tmp_path, log=print)
    except LedgerBusy as e:
        refused = str(e)
    else:
        refused = ""
    holder.close()
    time.sleep(1.0)
    for s in servers:
        s.shutdown()
    held = session.ledger_path("held", tmp_path)
    assert refused == f"{held}: another feeder holds this ledger; refusing to feed the same session"
    assert read_ledger(session.ledger_path("free", tmp_path)) == []
    assert received(tmp_path / "free.log") == []
    assert not feeder_stats_path(session, "free", tmp_path).exists()


def test_a_replay_session_records_its_arrival_horizon(tmp_path: Path) -> None:
    from synthetic import log, logs, span, traces, write_corpus

    t = 1790237400 * NS
    corpus = write_corpus(
        tmp_path / "horizon",
        [
            traces("cart", span("a" * 32, "s1", None, "GET", 2, t, 10_000)),
            logs("cart", log(t + 5 * NS, "x"), log(t + 100 * NS + 500_000_000, "y")),
        ],
    )
    session, _ = start_session(corpus, ("b4",), "replay", repo=tmp_path)
    reloaded = Session.load(session.dir(tmp_path) / "session.json")
    legacy = json.loads((session.dir(tmp_path) / "session.json").read_text())
    legacy.pop("arrival_horizon_s")
    (tmp_path / "legacy.json").write_text(json.dumps(legacy))
    assert (session.arrival_horizon_s, reloaded.arrival_horizon_s) == (96, 96)
    assert Session.load(tmp_path / "legacy.json").arrival_horizon_s == 3600


def test_a_frame_the_normaliser_emptied_is_never_due(tmp_path: Path) -> None:
    from synthetic import span, traces, write_corpus

    t = 1790237400 * NS
    corpus = write_corpus(
        tmp_path / "emptied",
        [
            traces("flagd", span("f" * 32, "f1", None, "resolve", 2, t - 60 * NS, 1_000)),
            traces("cart", span("a" * 32, "s1", None, "GET", 2, t, 10_000)),
        ],
    )
    archive = Archive.open(corpus)
    frames = [e for seg in archive.segments() for e in archive.index(seg.signal, seg.stream, seg.seq)]
    assert [(f.records, f.max_event_ns) for f in frames] == [(0, 0), (1, t + 10 * NS)]

    now = 1_900_000_000 * NS
    session, _ = start_session(corpus, ("b4",), "replay", repo=tmp_path, now_ns=now, lead_s=60)
    first_due = t + 10 * NS + session.shift_s * NS
    assert now + 60 * NS <= first_due < now + 120 * NS
    assert [e.records for _, e in selected_frames(archive, None)] == [1]
    assert [e.records for _, e in archive.merged() if session.selects(e)] == [1]


def test_a_follow_session_derives_its_arrival_horizon_from_what_it_sent(tmp_path: Path) -> None:
    """Follow mode (live capture, shift 0) cannot know its horizon when it starts. Ways that could go wrong: the
    report carries no horizon, so a B4 probe on the session raises; the horizon counts frames the stack was never
    sent; the gate keeps reading the session's None instead of the report's value; the lateness the frames were
    sent with (a stalled stack files rows later still) is left out of the range B4 is asked for, or a report
    written before it was measured no longer gives the span alone."""
    from synthetic import log, logs, span, traces, write_corpus

    from otb.gate import arrival_horizon
    from otb.stacks import registry
    from otb.stacks.context import Response, StackContext

    t = 1790237400 * NS
    corpus = write_corpus(
        tmp_path / "horizon",
        [
            traces("cart", span("a" * 32, "s1", None, "GET", 2, t, 10_000)),
            logs("cart", log(t + 5 * NS, "x"), log(t + 100 * NS + 500_000_000, "y")),
        ],
    )
    session, _ = start_session(corpus, ("b4-parseable",), "follow", repo=tmp_path)
    assert (session.shift_s, session.arrival_horizon_s) == (0, None)
    before = stack_report(Archive(corpus), session, "b4-parseable", {}, session.ledger_path("b4-parseable", tmp_path))
    assert before.arrival_horizon_s is None, "a horizon before any frame was sent"

    port = free_port()
    server = serve(port, tmp_path / "collector.log", set(), 0, 0.0)
    (report,) = run_session(
        session, [target("b4-parseable", f"http://127.0.0.1:{port}")], tmp_path, log=lambda m: None
    ).values()
    server.shutdown()
    assert (report.finished, report.arrival_horizon_s) == (True, 96)
    # Following a corpus recorded on 2026-09-24 sends every frame weeks after it was due, so here the measured
    # lateness is that long; a live follow sends each frame seconds after its records.
    late = max(v.late_max_s or 0 for v in report.signals.values())
    assert late > 86400
    assert 96 <= report.query_horizon_s <= 96 + math.ceil(late), "the span plus the lateness the frames were sent with"
    assert arrival_horizon(session, json.loads(json.dumps(report.to_json()))) == report.query_horizon_s
    legacy = {k: v for k, v in report.to_json().items() if k != "query_horizon_s"}
    assert arrival_horizon(session, legacy) == 96, "a report written before lateness was measured gives the span"

    class Answering(StackContext):
        def secret(self, name: str) -> str:
            return "x"

        def http(self, method: str, url: str, *, body: object = None, **_: object) -> Response:  # type: ignore[override]
            asked.append(body)
            return Response(200, b"[]")

    asked: list = []
    b4 = registry()["b4-parseable"]
    wall = session.shift.wall_window(Window(EventNs(t), EventNs(t + 60 * NS)))
    for horizon in (arrival_horizon(session, legacy), 126, arrival_horizon(session, report.to_json())):
        ctx = Answering(b4, tmp_path, arrival_horizon_s=horizon)
        assert b4.probes._rows(ctx, "SELECT count(*) FROM logs $WHERE", "logs", wall) == []
    assert asked[0]["endTime"] == "2026-09-24T08:13:00.000Z", (
        "window end 08:11:00 + late 10 s + horizon 96 s is 08:12:46, rounded up to the minute (Parseable defect 8)"
    )
    assert asked[1]["endTime"] == "2026-09-24T08:14:00.000Z", (
        "a stack that took its frames in 30 s late is asked 30 s further: 08:11:00 + 126 s is 08:13:06, so 08:14"
    )
    filed = datetime.fromtimestamp(report.last_outcome_ns / NS, UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    assert asked[2]["endTime"] >= filed, "the measured horizon reaches the time the stack took the last frame"
    assert len({a["startTime"] for a in asked}) == 1 and len({a["query"] for a in asked}) == 1


def test_a_session_is_not_resumed_onto_wiped_stacks(tmp_path: Path) -> None:
    from synthetic import span, traces, write_corpus

    from otb.ingest import record_wipe, wiped_since

    t = 1790237400 * NS
    corpus = write_corpus(tmp_path / "wipe", [traces("cart", span("a" * 32, "s1", None, "GET", 2, t, 10))])
    first, resumed = start_session(corpus, ("b4",), "replay", repo=tmp_path, now_ns=1_900_000_000 * NS)
    assert not resumed
    again, resumed = start_session(corpus, ("b4",), "replay", repo=tmp_path, now_ns=1_900_000_100 * NS)
    assert (again.id, resumed) == (first.id, True)

    record_wipe("b4", tmp_path, now_ns=1_900_000_200 * NS)
    assert wiped_since(first, tmp_path) == ["b4"]
    fresh, resumed = start_session(corpus, ("b4",), "replay", repo=tmp_path, now_ns=1_900_000_300 * NS)
    assert not resumed and fresh.id != first.id and wiped_since(fresh, tmp_path) == []


def test_a_follow_session_takes_the_sealed_corpus_id(tmp_path: Path) -> None:
    import orjson
    from synthetic import SOURCE, span, traces

    from otb.archive import ArchiveWriter, Header, NormaliserStamp
    from otb.ingest import adopt_sealed_corpus
    from otb.normalise import normalise

    root = tmp_path / "live"
    w = ArchiveWriter(
        root, Header(root.name, ("default",), SOURCE, NormaliserStamp.current(), rotate_bytes=1 << 20), rotate_s=None
    )
    signal, doc = traces("cart", span("a" * 32, "s1", None, "GET", 2, 1790237400 * NS, 10))
    w.append(signal, normalise(signal, orjson.dumps(doc)), received_ns=0)
    session, _ = start_session(root, ("b4",), "follow", repo=tmp_path)
    assert session.corpus_id == "unsealed"
    assert adopt_sealed_corpus(session, tmp_path).corpus_id == "unsealed", "adopted before the capture sealed"

    other = tmp_path / "other-live"
    w2 = ArchiveWriter(
        other, Header(other.name, ("default",), SOURCE, NormaliserStamp.current(), rotate_bytes=1 << 20), rotate_s=None
    )
    w2.close()
    other_session, other_resumed = start_session(other, ("b4",), "follow", repo=tmp_path, now_ns=1_900_000_000 * NS)
    assert (other_resumed, other_session.id != session.id) == (False, True), "resumed another live capture's session"

    m = w.seal(scenario_sha256=None, schedule_sha256=None)
    w.close()
    again, resumed = start_session(root, ("b4",), "follow", repo=tmp_path, now_ns=1_900_000_100 * NS)
    assert (again.id, resumed) == (session.id, True), "a follow ingest restarted after the seal began a new session"
    adopted = adopt_sealed_corpus(session, tmp_path)
    assert adopted.corpus_id == m.corpus_id
    assert Session.load(session.dir(tmp_path) / "session.json").corpus_id == m.corpus_id
    assert adopt_sealed_corpus(adopted, tmp_path) == adopted


def live_writer(root: Path):
    from synthetic import SOURCE

    from otb.archive import ArchiveWriter, Header, NormaliserStamp

    header = Header(root.name, ("default",), SOURCE, NormaliserStamp.current(), rotate_bytes=1 << 20)
    return ArchiveWriter(root, header, rotate_s=None)


def append_spans(writer, n: int, first: int) -> None:
    import orjson
    from synthetic import span, traces

    from otb.normalise import normalise

    for i in range(first, first + n):
        signal, doc = traces("cart", span(f"{i:032x}", f"{i:016x}", None, "GET", 2, 1790237400 * NS + i * NS, 10))
        writer.append(signal, normalise(signal, orjson.dumps(doc)), received_ns=0)


def test_a_follow_session_is_resumed_after_its_capture_sealed(tmp_path: Path, artifacts: Path) -> None:
    root = tmp_path / "live"
    writer = live_writer(root)
    append_spans(writer, 3, 0)
    port = free_port()
    log = tmp_path / "collector.log"
    server = serve(port, log, set(), 0, 0.0)
    first = drive(tmp_path, port, names=("b4",), corpus=root, mode="follow")
    started = json.loads(first.stdout.readline())
    session_path = tmp_path / "state/sessions" / started["session"] / "session.json"
    ledger_path = Session.load(session_path).ledger_path("b4", tmp_path)
    while outcomes(ledger_path) < 3:
        assert first.poll() is None, "the follow feeder exited before the capture sealed"
        time.sleep(0.05)
    first.send_signal(signal.SIGKILL)
    first.wait()
    seconds_until_free(ledger_path)
    unsealed_id = Session.load(session_path).corpus_id

    append_spans(writer, 2, 3)
    m = writer.seal(scenario_sha256=None, schedule_sha256=None)
    writer.close()
    time.sleep(1.1)  # session ids carry the second they started: a rerun hours later must not collide by luck
    second = drive(tmp_path, port, names=("b4",), corpus=root, mode="follow")
    resumed = json.loads(second.stdout.readline())
    second.wait(timeout=120)
    server.shutdown()

    session = Session.load(session_path)
    want = expected_shas(Archive(root), session)
    arrivals = Counter(r["sha"] for r in received(log))
    result = {
        "frames_in_corpus": len(want),
        "sessions": sorted(d.name for d in (tmp_path / "state/sessions").iterdir()),
        "corpus_id_while_unsealed": unsealed_id,
        "rerun_resumed_same_session": resumed["resumed"] and resumed["session"] == started["session"],
        "rerun_shift_s": resumed["shift_s"],
        "rerun_exit_code": second.returncode,
        "every_frame_received_once": all(arrivals[sha] == 1 for sha in want.values())
        and sum(arrivals.values()) == len(want),
        "corpus_id_after_rerun_is_sealed_id": session.corpus_id == m.corpus_id,
        "done_marker_after_rerun": (session.dir(tmp_path) / "ingest" / "done.json").exists(),
    }
    update_artifact(artifacts, "follow_resume_after_seal", {k: v for k, v in result.items() if k != "sessions"})
    assert result == {
        "frames_in_corpus": 5,
        "sessions": [started["session"]],
        "corpus_id_while_unsealed": "unsealed",
        "rerun_resumed_same_session": True,
        "rerun_shift_s": 0,
        "rerun_exit_code": 0,
        "every_frame_received_once": True,
        "corpus_id_after_rerun_is_sealed_id": True,
        "done_marker_after_rerun": True,
    }


def test_counters_accumulate_across_a_collector_restart(tmp_path: Path, artifacts: Path) -> None:
    """The stand-in collector answers 503 to its first two requests. Its refusal counters are played by `probe`
    from its own log (COUNTS_PER_REFUSAL per refused record), and after both refusals it goes down for one reading
    and comes back with the counters at zero: the restart. The gate's judge, run on the baseline the session
    wrote, must still count every refusal the ledger accounts for."""
    from otb.gate import COUNTS_PER_REFUSAL, Plan, Point, judge_stack
    from otb.ingest import counters_path, write_counter_sample
    from otb.stacks import Exact, IngestCounters, ProbeError

    archive = Archive(MINI)
    session, _ = start_session(MINI, ("b4",), "replay", window=WINDOW, lead_s=LEAD_S, repo=tmp_path)
    records = {
        hashlib.sha256(replay_body(archive.read(r, e), e, session.shift)).hexdigest(): e.records
        for r, e in selected_frames(archive, session.window)
    }
    port = free_port()
    log = tmp_path / "collector.log"
    server = serve(port, log, set(), 2, 0.5)
    state = {"phase": "up", "offset": dict.fromkeys(("traces", "logs", "metrics"), 0)}

    def counters() -> dict[str, int]:
        refused = dict.fromkeys(("traces", "logs", "metrics"), 0)
        for r in received(log):
            if r["code"] == 503:
                refused[r["path"].rsplit("/", 1)[1]] += records[r["sha"]]
        return {s: COUNTS_PER_REFUSAL * n for s, n in refused.items()}

    def probe() -> Exact | ProbeError:
        live = counters()
        if state["phase"] == "up":
            if sum(1 for r in received(log) if r["code"] == 503) == 2:
                state["phase"] = "down"
            return Exact(live)
        if state["phase"] == "down":
            state["phase"], state["offset"] = "restarted", live
            return ProbeError("connection refused")
        return Exact({s: v - state["offset"][s] for s, v in live.items()})

    write_counter_sample(session, "b4", probe(), tmp_path)
    (report,) = run_session(
        session,
        [target("b4", f"http://127.0.0.1:{port}")],
        tmp_path,
        log=lambda m: None,
        sample=lambda: write_counter_sample(session, "b4", probe(), tmp_path),
        sample_every_s=0.5,
    ).values()
    server.shutdown()

    readings = [json.loads(x) for x in counters_path(session, "b4", tmp_path).read_text().splitlines()]
    baseline = json.loads((session.dir(tmp_path) / "ingest" / "b4.counters_start.json").read_text())
    point = Point.of(IngestCounters(), None)
    plan = Plan(seed=0, session=session.id, corpus_id=session.corpus_id, windows={}, points=(point,))
    verdict = judge_stack(plan, {point.id: 0}, {point.id: probe()}, report.to_json(), baseline)
    checks = verdict["checks"]
    naive = judge_stack(plan, {point.id: 0}, {point.id: probe()}, report.to_json(), readings[0]["counters"])
    refused = {s: v.refused_records for s, v in report.signals.items()}
    result = {
        "refused_records": refused,
        "readings": len(readings),
        "readings_while_down": sum(1 for r in readings if r["counters"] is None),
        "first_reading": readings[0]["counters"],
        "last_reading": readings[-1]["counters"],
        "baseline": baseline,
        "gate": {s: (checks[f"{point.id}:{s}"]["status"], checks[f"{point.id}:{s}"]["got"]) for s in refused},
        "gate_family": verdict["families"]["ingest.counters"],
        "gate_family_with_first_reading_as_baseline": naive["families"]["ingest.counters"],
    }
    update_artifact(artifacts, "counters_restart", {k: v for k, v in result.items() if k != "readings"})
    assert sum(refused.values()) > 0, "the stand-in must have refused records"
    assert result["readings"] >= 4 and result["readings_while_down"] == 1
    assert result["first_reading"] == {"logs": 0, "metrics": 0, "traces": 0}
    assert result["last_reading"] == {"logs": 0, "metrics": 0, "traces": 0}, "the restarted counters read zero"
    assert result["baseline"] == {s: -COUNTS_PER_REFUSAL * n for s, n in refused.items()}
    assert result["gate"] == {s: ("pass", COUNTS_PER_REFUSAL * n) for s, n in refused.items()}
    assert result["gate_family"] == "pass"
    assert result["gate_family_with_first_reading_as_baseline"] == "fail", "the baseline record_counters used to write"
