"""Ingestion: the archive is the bus. One feeder process per stack reads the archive in due order and POSTs each
frame to that stack's synchronous ingest collector, so a slow stack slows only its own feeder, and a stack's frame
preparation uses its own core (run_session says why processes, not threads).

A session (state/sessions/<id>/session.json) fixes the corpus, mode, stacks, optional recording-time window and
the shift. Replay shifts every *UnixNano by whole minutes chosen so the first frame is due lead_s after the
session starts, and sends each frame at its due time (1x); follow tails a live archive at shift 0.

Each (session, stack) has an append-only ledger, the feeder's only state:
  {"ev": "start"}                     a feeder process began; intents before it without an outcome are in doubt
  {"ev": "intent", "f": ...}          written before the first POST of a frame
  {"ev": "retry", "f": ..., ...}      a 429, 5xx or transport error; the feeder backs off and retries. A transport
                                      error after the request was sent sets in_doubt: the stack may have stored it
  {"ev": "outcome", "f": ..., ...}    "ok" (2xx) or "rejected" (other 4xx, with the response body); fsynced
Resume skips frames with an outcome and resends the rest; a frame in doubt may now be stored twice, which the
report names. One feeder per (session, stack) at a time: the ledger is held under an exclusive flock.
StackReport is a pure function of the archive index, the session and the ledger. The ledger is read in one
streamed pass (scan_ledger) into per-frame arrays (Outcomes), so resuming or reporting a 72-hour follow session's
ledger of millions of lines costs tens of bytes a frame, not a kilobyte a line.

Each (session, stack) also has a counters file, <stack>.counters.jsonl: one reading of the collector's refusal
counters at the session's start, every COUNTERS_EVERY_S while feeding, and when the feeders exit. The counters
restart from zero with their collector, so the gate's baseline (<stack>.counters_start.json) is folded from every
reading (counters_baseline), not copied from the first.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import itertools
import json
import math
import multiprocessing
import multiprocessing.connection
import os
import resource
import signal as signal_mod
import sys
import threading
import time
import urllib.error
import urllib.request
from array import array
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import orjson

from otb.archive import SIGNALS, Archive, FrameEntry, FrameRef, Signal, _read_index
from otb.cli import command
from otb.host import describe as describe_host
from otb.host import peak_rss_bytes
from otb.timebase import NS, EventNs, Shift, Window
from otb.timeshift import Band, recording_days, recording_time_left, shift_body, shift_clears

if TYPE_CHECKING:
    from otb.stacks import Probe, StackDef

REPO = Path(__file__).resolve().parent.parent
Mode = Literal["replay", "follow"]
Status = Literal["ok", "rejected"]
HTTP_TIMEOUT_S = 120
BACKOFF_MAX_S = 30.0
BODY_KEEP = 2000
COUNTERS_EVERY_S = 300.0
LEGACY_ARRIVAL_HORIZON_S = 3600  # what B4 was probed with before sessions recorded their own horizon


def sessions_dir(repo: Path = REPO) -> Path:
    return repo / "state" / "sessions"


def frame_name(ref: FrameRef) -> str:
    return f"{ref.signal}/{ref.stream}/{ref.seq:06d}#{ref.index}"


def replay_body(raw: bytes, e: FrameEntry, shift: Shift) -> bytes:
    """The bytes a replay sends for a frame: its `*UnixNano` fields and its in-band time strings moved by the
    shift (otb.timeshift)."""
    return shift_body(raw, shift.seconds, Band.of(e.min_event_ns, e.max_event_ns))[0]


# ---------- session ----------


@dataclass(frozen=True, slots=True)
class Session:
    id: str
    corpus: str  # as given on the command line, repo-relative when inside the repo
    corpus_id: str
    mode: Mode
    shift_s: int
    lead_s: int
    window: Window | None  # recording time: replay only frames due in [start, end)
    stacks: tuple[str, ...]
    created_utc: str
    arrival_horizon_s: int | None  # longest event-time span of one selected frame; None in follow mode, where
    # frames arrive after the session starts and each stack's report derives it (StackReport.arrival_horizon_s)

    @property
    def shift(self) -> Shift:
        return Shift(self.shift_s)

    def selects(self, e: FrameEntry) -> bool:
        return in_window(self.window, e)

    def to_json(self) -> bytes:
        d = asdict(self)
        d["window"] = None if self.window is None else [self.window.start, self.window.end]
        return json.dumps(d, indent=1).encode() + b"\n"

    @staticmethod
    def load(path: Path) -> Session:
        d = json.loads(path.read_text())
        w = d["window"]
        return Session(
            id=d["id"],
            corpus=d["corpus"],
            corpus_id=d["corpus_id"],
            mode=d["mode"],
            shift_s=int(d["shift_s"]),
            lead_s=int(d["lead_s"]),
            window=None if w is None else Window(EventNs(w[0]), EventNs(w[1])),
            stacks=tuple(d["stacks"]),
            created_utc=d["created_utc"],
            arrival_horizon_s=d.get("arrival_horizon_s", LEGACY_ARRIVAL_HORIZON_S),
        )

    def dir(self, repo: Path = REPO) -> Path:
        return sessions_dir(repo) / self.id

    def ledger_path(self, stack: str, repo: Path = REPO) -> Path:
        return self.dir(repo) / "ingest" / f"{stack}.ledger.jsonl"


def in_window(window: Window | None, e: FrameEntry) -> bool:
    """Whether a session sends this frame. A frame the normaliser emptied (every record scrubbed) is kept in the
    archive as the batch that arrived, but it has no event time, so it is never due and never sent."""
    return e.records > 0 and (window is None or window.start <= e.max_event_ns < window.end)


def selected_frames(archive: Archive, window: Window | None) -> Iterator[tuple[FrameRef, FrameEntry]]:
    """The frames a session sends, in storage order (cheap: index files only)."""
    for seg in archive.segments():
        for e in _read_index(archive.root / seg.index_path):
            if in_window(window, e):
                yield FrameRef(seg.signal, seg.stream, seg.seq, e.index), e


def arrival_horizon_s(archive: Archive, window: Window | None) -> int:
    """A frame is sent when its newest record is due, so a record arrives up to its frame's event-time span after
    its own time: the longest such span over the selected frames, in whole seconds rounded up. An arrival-time
    store (B4) is queried that far past a window's end."""
    return horizon_of(e for _, e in selected_frames(archive, window))


def horizon_of(entries: Iterable[FrameEntry]) -> int:
    spans = [e.max_event_ns - e.min_event_ns for e in entries if e.min_event_ns]
    return math.ceil(max(spans, default=0) / NS)


def replay_shift(first_due_ns: int, now_ns: int, lead_s: int) -> int:
    """Whole minutes such that the first frame is due at least lead_s from now."""
    return math.ceil((now_ns + lead_s * NS - first_due_ns) / (60 * NS)) * 60


def _rel(path: Path, repo: Path) -> str:
    try:
        return str(path.resolve().relative_to(repo))
    except ValueError:
        return str(path.resolve())


def wipes_dir(repo: Path = REPO) -> Path:
    return repo / ".otb" / "wiped"


def record_wipe(stack: str, repo: Path = REPO, now_ns: int | None = None) -> None:
    """Called when a stack's volumes are deleted: every session that fed it before now no longer describes what it
    holds."""
    wipes_dir(repo).mkdir(parents=True, exist_ok=True)
    (wipes_dir(repo) / stack).write_text(f"{time.time_ns() if now_ns is None else now_ns}\n")


def wiped_since(s: Session, repo: Path = REPO) -> list[str]:
    """The session's stacks whose volumes were deleted after the session was created."""
    created = int(datetime.strptime(s.created_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()) * NS
    return [x for x in s.stacks if (f := wipes_dir(repo) / x).exists() and int(f.read_text()) >= created]


def wait_for_archive(corpus: Path, wait_s: float, poll_s: float = 0.5) -> None:
    """Follow mode starts with the capture, before the gateway has written anything: frames that wait in the archive
    for a feeder that starts later arrive late at an arrival-time stack."""
    deadline = time.monotonic() + wait_s
    while True:
        if (corpus / "manifest.yaml").exists():
            return
        if time.monotonic() >= deadline:
            raise SystemExit(f"{corpus}: no archive after {wait_s:g} s; is the capture running?")
        time.sleep(poll_s)


def find_unfinished(
    corpus: str, corpus_id: str, mode: Mode, stacks: tuple[str, ...], window: Window | None, repo: Path
) -> Session | None:
    """A session is resumed only while its stacks still hold what its ledgers say they were sent. A follow session
    started before its capture sealed records the corpus as unsealed, so it is matched by the corpus's path: a
    restart after the seal resumes it, and another live capture does not."""
    root = sessions_dir(repo)
    if not root.exists():
        return None
    for d in sorted(root.iterdir(), reverse=True):
        if not (d / "session.json").exists() or (d / "ingest" / "done.json").exists():
            continue
        s = Session.load(d / "session.json")
        same_corpus = s.corpus == corpus if s.corpus_id == "unsealed" else s.corpus_id == corpus_id
        if same_corpus and (s.mode, s.stacks, s.window) == (mode, stacks, window) and not wiped_since(s, repo):
            return s
    return None


def start_session(
    corpus: Path,
    stacks: tuple[str, ...],
    mode: Mode,
    *,
    window: Window | None = None,
    lead_s: int = 60,
    repo: Path = REPO,
    now_ns: int | None = None,
    before_new: Callable[[], None] | None = None,
) -> tuple[Session, bool]:
    """Returns (session, resumed). An unfinished session with the same corpus, mode, stacks and window is resumed
    with its shift unchanged: a resumed replay must not pick a new shift. A follow session whose capture sealed
    while no feeder ran takes the sealed corpus id as it resumes."""
    archive = Archive.open(corpus, verify="none" if mode == "follow" else "index")
    m = archive.manifest
    corpus_id = m.corpus_id or "unsealed"
    found = find_unfinished(_rel(corpus, repo), corpus_id, mode, stacks, window, repo)
    if found is not None:
        return adopt_sealed_corpus(found, repo), True
    if before_new is not None:
        before_new()
    now_ns = time.time_ns() if now_ns is None else now_ns
    shift_s, horizon = 0, None
    if mode == "replay":
        dues = [e.max_event_ns for _, e in selected_frames(archive, window)]
        if not dues:
            raise SystemExit(f"{corpus}: no frames are due inside the window")
        shift_s = replay_shift(min(dues), now_ns, lead_s)
        horizon = arrival_horizon_s(archive, window)
    stamp = datetime.fromtimestamp(now_ns / NS, UTC)
    s = Session(
        id=f"{m.name}-{mode}-{stamp:%Y%m%dT%H%M%SZ}",
        corpus=_rel(corpus, repo),
        corpus_id=corpus_id,
        mode=mode,
        shift_s=shift_s,
        lead_s=lead_s,
        window=window,
        stacks=stacks,
        created_utc=stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        arrival_horizon_s=horizon,
    )
    (s.dir(repo) / "ingest").mkdir(parents=True, exist_ok=True)
    (s.dir(repo) / "session.json").write_bytes(s.to_json())
    return s, False


# ---------- ledger ----------


def _ref_json(ref: FrameRef) -> list:
    return [ref.signal, ref.stream, ref.seq, ref.index]


def _ref(v: list) -> FrameRef:
    return FrameRef(v[0], v[1], int(v[2]), int(v[3]))


def iter_jsonl(path: Path) -> Iterator[dict]:
    """Complete lines only, streamed: a torn last line from a kill is ignored (and truncated by the next writer)."""
    if not path.exists():
        return
    with path.open("rb") as f:
        for line in f:
            if not line.endswith(b"\n"):
                return
            if line != b"\n":
                yield orjson.loads(line)


def read_ledger(path: Path) -> list[dict]:
    """Every complete line in memory: for tests and small ledgers. Feeders and reports use scan_ledger."""
    return list(iter_jsonl(path))


def _complete_length(path: Path) -> int:
    """Bytes up to and including the last newline, read from the tail: a torn last line is not part of the ledger."""
    with path.open("rb") as f:
        pos = f.seek(0, os.SEEK_END)
        while pos > 0:
            step = min(1 << 16, pos)
            pos -= step
            f.seek(pos)
            cut = f.read(step).rfind(b"\n")
            if cut >= 0:
                return pos + cut + 1
    return 0


OK, REJECTED = 1, 2


class _SegmentOutcomes:
    """One segment's frames by index: status (0 none, OK, REJECTED), lateness, records and time_left (-1: None)
    of each frame's last outcome line."""

    __slots__ = ("late_ns", "records", "status", "time_left")

    def __init__(self) -> None:
        self.status = bytearray()
        self.late_ns = array("q")
        self.records = array("I")
        self.time_left = array("i")

    def set(self, index: int, status: int, late_ns: int, records: int, time_left: int | None) -> None:
        grow = index + 1 - len(self.status)
        if grow > 0:
            self.status.extend(bytes(grow))
            self.late_ns.frombytes(bytes(self.late_ns.itemsize * grow))
            self.records.frombytes(bytes(self.records.itemsize * grow))
            self.time_left.frombytes(b"\xff" * (self.time_left.itemsize * grow))
        self.status[index] = status
        self.late_ns[index] = late_ns
        self.records[index] = records
        self.time_left[index] = -1 if time_left is None else time_left


class Outcomes:
    """The last outcome of every frame in a ledger, as per-segment arrays indexed by frame index: 17 bytes a
    frame, so a 4 M-frame ledger folds into about 70 MB however many lines it has."""

    def __init__(self) -> None:
        self._segments: dict[tuple[Signal, str, int], _SegmentOutcomes] = {}
        self.rejected: dict[FrameRef, tuple[int, str | None]] = {}  # HTTP status and body of each rejected frame
        self.frames = 0  # frames with an outcome

    def record(self, ref: FrameRef, line: dict) -> None:
        seg = self._segments.setdefault((ref.signal, ref.stream, ref.seq), _SegmentOutcomes())
        if ref not in self:
            self.frames += 1
        ok = line["status"] == "ok"
        late_ns = line["sent_ns"] - line["due_ns"]
        seg.set(ref.index, OK if ok else REJECTED, late_ns, line["records"], line.get("time_left"))
        if ok:
            self.rejected.pop(ref, None)
        else:
            self.rejected[ref] = (line["http"], line["body"])

    def __contains__(self, ref: FrameRef) -> bool:
        return self.get(ref) is not None

    def get(self, ref: FrameRef) -> tuple[int, int] | None:
        """(status, lateness in ns) of the frame's last outcome, or None without one."""
        seg = self._segments.get((ref.signal, ref.stream, ref.seq))
        if seg is None or ref.index >= len(seg.status) or not seg.status[ref.index]:
            return None
        return seg.status[ref.index], seg.late_ns[ref.index]

    def of_signal(self, signal: str) -> Iterator[tuple[FrameRef, int, int, int, int]]:
        """Every frame of the signal with an outcome, in storage order: (ref, status, late_ns, records, time_left),
        time_left -1 for None."""
        for key in sorted(k for k in self._segments if k[0] == signal):
            seg = self._segments[key]
            for i, status in enumerate(seg.status):
                if status:
                    yield FrameRef(*key, i), status, seg.late_ns[i], seg.records[i], seg.time_left[i]


@dataclass
class LedgerScan:
    """One streamed pass over a ledger. Memory follows the frames with an outcome (Outcomes) and the frames in
    doubt, not the line count."""

    outcomes: Outcomes = field(default_factory=Outcomes)
    in_doubt: set[FrameRef] = field(default_factory=set)  # ever sent without a known outcome: may be stored twice
    retries: dict[str, int] = field(default_factory=lambda: dict.fromkeys(SIGNALS, 0))
    refused: dict[str, int] = field(default_factory=lambda: dict.fromkeys(SIGNALS, 0))  # records answered 5xx
    last_outcome_ns: int | None = None

    def unresolved(self) -> set[FrameRef]:
        """Frames in doubt that still have no outcome: the next feeder resends them."""
        return {r for r in self.in_doubt if r not in self.outcomes}


def scan_ledger(path: Path) -> LedgerScan:
    scan = LedgerScan()
    open_intents: set[FrameRef] = set()
    for line in iter_jsonl(path):
        ev = line["ev"]
        if ev == "start":
            scan.in_doubt |= open_intents
            open_intents = set()
        elif ev == "intent":
            open_intents.add(_ref(line["f"]))
        elif ev == "retry":
            scan.retries[line["f"][0]] += 1
            if line["http"] is not None and line["http"] >= 500:
                scan.refused[line["f"][0]] += line["records"]
            if line.get("in_doubt"):
                scan.in_doubt.add(_ref(line["f"]))
        elif ev == "outcome":
            ref = _ref(line["f"])
            scan.outcomes.record(ref, line)
            open_intents.discard(ref)
            scan.last_outcome_ns = max(scan.last_outcome_ns or 0, line["done_ns"])
    scan.in_doubt |= open_intents
    return scan


class LedgerBusy(Exception):
    pass


class Ledger:
    """Append-only JSONL, one writer process (its feeder) under an exclusive flock; threads of that feeder share
    one lock. Outcome lines are fsynced: an acknowledged frame must not be resent after a power loss."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._f = path.open("ab")
        try:
            fcntl.flock(self._f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._f.close()
            raise LedgerBusy(f"{path}: another feeder holds this ledger; refusing to feed the same session") from None
        end = _complete_length(path)
        if end != path.stat().st_size:
            self._f.truncate(end)
        scan = scan_ledger(path)
        self.done = scan.outcomes
        self.in_doubt = scan.unresolved()
        self._lock = threading.Lock()

    def write(self, obj: dict) -> None:
        line = orjson.dumps(obj) + b"\n"
        with self._lock:
            self._f.write(line)
            self._f.flush()
            if obj["ev"] == "outcome":
                os.fsync(self._f.fileno())

    def close(self) -> None:
        self._f.close()


# ---------- feeder ----------


@dataclass(frozen=True, slots=True)
class Target:
    """Where a feeder sends and how lateness is judged: the ingest part of a StackDef."""

    stack: str
    url: str  # collector OTLP/HTTP base URL
    paths: dict[Signal, str]
    max_inflight: int
    max_late_s: dict[str, float | None]

    @staticmethod
    def of(stack: StackDef) -> Target:
        i = stack.ingest
        return Target(stack.id, i.url, dict(i.paths), i.max_inflight, dict(stack.max_late_s))


@dataclass
class Feeder:
    archive: Archive
    target: Target
    session: Session
    ledger: Ledger
    stop: threading.Event = field(default_factory=threading.Event)
    log: Callable[[str], None] = print
    frames_submitted: int = 0
    bytes_submitted: int = 0  # shifted body bytes handed to the senders

    def run(self) -> None:
        shift = self.session.shift
        span = self.archive.manifest.window
        clears = span is not None and shift_clears(shift.seconds, span.start, span.end)
        days = recording_days(span.start, span.end) if clears and span is not None else frozenset()
        self.ledger.write({"ev": "start", "at_ns": time.time_ns(), "in_doubt": len(self.ledger.in_doubt)})
        slots = threading.BoundedSemaphore(self.target.max_inflight)
        with ThreadPoolExecutor(self.target.max_inflight, thread_name_prefix=f"feed-{self.target.stack}") as pool:
            for ref, e in self.archive.merged(skip=self.ledger.done.__contains__, follow=self.session.mode == "follow"):
                if not self.session.selects(e):
                    continue
                due_ns = shift.wall(EventNs(e.max_event_ns))
                while (wait := (due_ns - time.time_ns()) / NS) > 0:
                    if self.stop.wait(min(wait, 1.0)):
                        return
                body = replay_body(self.archive.read(ref, e), e, shift)
                left = recording_time_left(body, days, Band.of(e.min_event_ns, e.max_event_ns)) if clears else None
                while not slots.acquire(timeout=1.0):
                    if self.stop.is_set():
                        return
                if self.stop.is_set():
                    slots.release()
                    return
                fut = pool.submit(self._send, ref, e, due_ns, body, left)
                fut.add_done_callback(lambda _f: slots.release())
                self.frames_submitted += 1
                self.bytes_submitted += len(body)

    def _send(self, ref: FrameRef, e: FrameEntry, due_ns: int, body: bytes, time_left: int | None) -> None:
        f = _ref_json(ref)
        self.ledger.write({"ev": "intent", "f": f, "due_ns": due_ns, "at_ns": time.time_ns()})
        url = self.target.url + self.target.paths[ref.signal]
        attempt, backoff = 0, 1.0
        while True:
            attempt += 1
            sent_ns = time.time_ns()
            status, text, error, in_doubt = _post(url, body)
            if status is not None and not retryable(status):
                ok = status < 300
                self.ledger.write(
                    {
                        "ev": "outcome",
                        "f": f,
                        "status": "ok" if ok else "rejected",
                        "http": status,
                        "attempts": attempt,
                        "due_ns": due_ns,
                        "sent_ns": sent_ns,
                        "done_ns": time.time_ns(),
                        "records": e.records,
                        "time_left": time_left,
                        "body": None if ok else text[:BODY_KEEP],
                    }
                )
                if not ok:
                    self.log(f"{self.target.stack}: {frame_name(ref)} rejected with HTTP {status}: {text[:200]}")
                return
            self.ledger.write(
                {
                    "ev": "retry",
                    "f": f,
                    "attempt": attempt,
                    "http": status,
                    "error": (error or text)[:BODY_KEEP],
                    "in_doubt": in_doubt,
                    "records": e.records,
                    "at_ns": sent_ns,
                }
            )
            if self.stop.wait(backoff):
                return
            backoff = min(backoff * 2, BACKOFF_MAX_S)


def retryable(status: int) -> bool:
    """OTLP/HTTP: 429, 502, 503 and 504 are retryable; amendment 4 retries every 5xx. Any other non-2xx is a
    rejection the feeder records and moves past."""
    return status == 429 or status >= 500


def _post(url: str, body: bytes) -> tuple[int | None, str, str | None, bool]:
    """(status, response text, transport error, in doubt). urllib wraps a failure while sending in URLError; an
    error after the request went out (a read timeout, a reset) comes unwrapped, and the stack may have stored
    the frame: in doubt."""
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:
            return r.status, r.read().decode(errors="replace"), None, False
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode(errors="replace"), None, False
    except urllib.error.URLError as err:
        return None, "", f"{type(err.reason).__name__}: {err.reason}", False
    except (OSError, TimeoutError) as err:
        return None, "", f"{type(err).__name__}: {err}", True


# ---------- report ----------


@dataclass(frozen=True, slots=True)
class SignalReport:
    frames_expected: int
    frames_ok: int
    frames_rejected: int
    frames_missing: int
    records_expected: int
    records_ok: int
    late_bound_s: float | None
    late_frames: int
    late_p50_s: float | None
    late_p99_s: float | None
    late_max_s: float | None
    retries: int
    refused_records: int  # records of attempts the collector answered with a 5xx: it counted them as refused
    rejected_records: int  # records of frames the stack rejected for good (4xx), which its collector also counted
    time_strings_left: int | None  # recording-time strings in sent bodies (otb.timeshift); None: not checkable
    complete: bool  # every frame ok, none later than the bound, no recording-time string left


LOCALISED_KINDS = ("rejected", "missing", "in_doubt")
# Frames of one kind and signal whose event spans the report lists, so the gate can fail each as its own windowed
# check. Past this many the list is empty and the gate fails the whole signal, as it did before the spans existed:
# an ingest that lost that much of a signal is not localisable in any useful way.
MAX_LOCALISED_FRAMES = 2000


@dataclass(frozen=True, slots=True)
class StackReport:
    stack: str
    session: str
    finished: bool  # every selected frame has an outcome
    complete: bool  # every signal complete
    signals: dict[str, SignalReport]
    rejected: list[dict]  # {frame, http, body}
    in_doubt: list[str]  # sent without a known outcome (a feeder died, or a transport error after sending), then
    # resent: may be stored twice
    late: dict[str, list[str]]  # first frames past the bound, per signal
    late_spans: dict[str, list[dict]]  # every late frame: its records' event span (recording time) and lateness
    last_outcome_ns: int | None
    arrival_horizon_s: int | None  # arrival_horizon_s over the frames this stack was sent; None before any
    # kind (LOCALISED_KINDS) -> signal -> {frame, start_ns, end_ns}: the event span of every rejected, missing and
    # in-doubt frame, capped per signal by MAX_LOCALISED_FRAMES (then empty)
    frame_spans: dict[str, dict[str, list[dict]]] = field(default_factory=dict)
    # How far past its event time a record may have reached the stack: its frame's event-time span plus the
    # lateness the frame was sent with, the longest over the frames answered ok, in whole seconds rounded up. An
    # arrival-time store (B4) is queried that far past a window's end. None before any frame was answered.
    query_horizon_s: int | None = None

    def to_json(self) -> dict:
        return asdict(self)


def _quantile(sorted_ns: list[int], q: float) -> float | None:
    if not sorted_ns:
        return None
    return round(sorted_ns[min(len(sorted_ns) - 1, int(q * len(sorted_ns)))] / NS, 3)


def _late(late_ns: int, bound_s: float | None) -> bool:
    return bound_s is not None and late_ns / NS > bound_s


class _LocalisedFrames:
    """The frames of one kind and signal the report lists, with their event spans: kept while there are at most
    MAX_LOCALISED_FRAMES of them, dropped past that, so the list never follows the frame count."""

    __slots__ = ("frames", "over")

    def __init__(self) -> None:
        self.frames: list[tuple[FrameRef, int, int]] = []
        self.over = False

    def add(self, ref: FrameRef, e: FrameEntry) -> None:
        if self.over:
            return
        self.frames.append((ref, e.min_event_ns, e.max_event_ns))
        if len(self.frames) > MAX_LOCALISED_FRAMES:
            self.frames, self.over = [], True

    def spans(self) -> list[dict]:
        """In frame order; half-open: regions overlap on `<`, so the end sits one nanosecond past the newest record."""
        return [{"frame": frame_name(r), "start_ns": lo, "end_ns": hi + 1} for r, lo, hi in sorted(self.frames)]


def stack_report(
    archive: Archive, session: Session, stack: str, max_late_s: dict[str, float | None], ledger_path: Path
) -> StackReport:
    """Pure function of the archive index, the session and the ledger. The ledger is folded once into per-frame
    arrays (scan_ledger) and the index is read a segment at a time, so memory follows the frame count."""
    scan = scan_ledger(ledger_path)
    expected: dict[str, list[int]] = {s: [0, 0] for s in SIGNALS}
    late_entries: dict[FrameRef, tuple[int, int]] = {}  # event span of each late frame the index holds
    localised = {(k, s): _LocalisedFrames() for k in LOCALISED_KINDS for s in SIGNALS}
    horizon_ns = 0
    reach_ns: int | None = None
    for ref, e in selected_frames(archive, session.window):
        expected[ref.signal][0] += 1
        expected[ref.signal][1] += e.records
        if ref in scan.in_doubt:
            localised["in_doubt", ref.signal].add(ref, e)
        got = scan.outcomes.get(ref)
        if got is None:
            localised["missing", ref.signal].add(ref, e)
            continue
        span = e.max_event_ns - e.min_event_ns if e.min_event_ns else None
        if span is not None:
            horizon_ns = max(horizon_ns, span)
        if got[0] != OK:
            localised["rejected", ref.signal].add(ref, e)
            continue
        if span is not None:
            reach_ns = max(reach_ns or 0, span + max(0, got[1]))
        if _late(got[1], max_late_s.get(ref.signal)):
            late_entries[ref] = (e.min_event_ns, e.max_event_ns)
    signals: dict[str, SignalReport] = {}
    late: dict[str, list[str]] = {}
    late_spans: dict[str, list[dict]] = {}
    for s in SIGNALS:
        bound = max_late_s.get(s)
        n_ok = n_rej = records_ok = records_rej = 0
        left: int | None = None
        lates = array("q")
        late_by_ref: dict[FrameRef, int] = {}
        for ref, status, late_ns, records, time_left in scan.outcomes.of_signal(s):
            if status != OK:
                n_rej += 1
                records_rej += records
                continue
            n_ok += 1
            records_ok += records
            lates.append(late_ns)
            if time_left >= 0:
                left = (left or 0) + time_left
            if _late(late_ns, bound):
                late_by_ref[ref] = late_ns
        late[s] = [frame_name(r) for r in itertools.islice(late_by_ref, 20)]
        late_spans[s] = [
            {
                "frame": frame_name(r),
                "start_ns": late_entries[r][0],
                "end_ns": late_entries[r][1] + math.ceil(late_ns / NS) * NS,
                "late_s": round(late_ns / NS, 3),
            }
            for r, late_ns in late_by_ref.items()
            if r in late_entries
        ]
        missing = expected[s][0] - n_ok - n_rej
        ordered = sorted(lates)
        signals[s] = SignalReport(
            frames_expected=expected[s][0],
            frames_ok=n_ok,
            frames_rejected=n_rej,
            frames_missing=missing,
            records_expected=expected[s][1],
            records_ok=records_ok,
            late_bound_s=bound,
            late_frames=len(late_by_ref),
            late_p50_s=_quantile(ordered, 0.5),
            late_p99_s=_quantile(ordered, 0.99),
            late_max_s=round(ordered[-1] / NS, 3) if ordered else None,
            retries=scan.retries[s],
            refused_records=scan.refused[s],
            rejected_records=records_rej,
            time_strings_left=left,
            complete=missing == 0 and n_rej == 0 and not late_by_ref and n_ok == expected[s][0] and not left,
        )
    rejected = [
        {"frame": frame_name(r), "http": http, "body": body}
        for r, (http, body) in sorted(scan.outcomes.rejected.items())
    ]
    finished = all(v.frames_missing == 0 for v in signals.values())
    return StackReport(
        stack=stack,
        session=session.id,
        finished=finished,
        complete=all(v.complete for v in signals.values()),
        signals=signals,
        rejected=rejected,
        in_doubt=[frame_name(r) for r in sorted(scan.in_doubt)],
        late=late,
        late_spans=late_spans,
        last_outcome_ns=scan.last_outcome_ns,
        arrival_horizon_s=math.ceil(horizon_ns / NS) if scan.outcomes.frames else None,
        frame_spans={k: {s: localised[k, s].spans() for s in SIGNALS} for k in LOCALISED_KINDS},
        query_horizon_s=math.ceil(reach_ns / NS) if reach_ns is not None else None,
    )


# ---------- running a session ----------


def corpus_path(session: Session, repo: Path = REPO) -> Path:
    return Path(session.corpus) if Path(session.corpus).is_absolute() else repo / session.corpus


def feeder_stats_path(session: Session, stack: str, repo: Path = REPO) -> Path:
    """One line per feeder process that fed this stack in this session: its wall and CPU time, peak RSS, and the
    frames and bytes it handed to its senders."""
    return session.dir(repo) / "ingest" / f"{stack}.feeder.jsonl"


# Feeder process to parent, on its events pipe. The first message is "ready" (in-doubt frame count) or "busy".
Event = tuple[Literal["ready"], int] | tuple[Literal["busy"], str] | tuple[Literal["log"], str]


def feed_stack(session: Session, target: Target, repo: Path, ctl: Connection, events: Connection) -> None:
    """The body of one stack's feeder process. Everything it reads or writes is its own: its ledger (under the
    flock), its Archive reader, its HTTP connections and its feeder stats file. It shares nothing with the other
    stacks' processes; the parent only relays its log lines and reads its reports afterwards.

    It opens its ledger, answers "ready" or "busy", and sends nothing until the parent says "go", so a busy ledger
    on any stack refuses the whole session before a frame is sent."""
    stop = threading.Event()
    signal_mod.signal(signal_mod.SIGINT, lambda *_: stop.set())
    signal_mod.signal(signal_mod.SIGTERM, lambda *_: stop.set())
    lock = threading.Lock()

    def emit(event: Event) -> None:
        with lock, contextlib.suppress(OSError):
            events.send(event)

    try:
        ledger = Ledger(session.ledger_path(target.stack, repo))
    except LedgerBusy as e:
        emit(("busy", str(e)))
        return
    emit(("ready", len(ledger.in_doubt)))
    try:
        go = ctl.recv() == "go"
    except EOFError:
        go = False
    if not go:
        ledger.close()
        return
    threading.Thread(target=_exit_with_parent, args=(ctl,), daemon=True).start()
    feeder = Feeder(Archive(corpus_path(session, repo)), target, session, ledger, stop, lambda m: emit(("log", m)))
    started_ns, t0 = time.time_ns(), time.monotonic()
    try:
        feeder.run()
    finally:
        ru = resource.getrusage(resource.RUSAGE_SELF)
        wall = time.monotonic() - t0
        cpu = ru.ru_utime + ru.ru_stime
        stats = {
            "pid": os.getpid(),
            "mode": session.mode,
            "started_utc": datetime.fromtimestamp(started_ns / NS, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "wall_s": round(wall, 3),
            "cpu_user_s": round(ru.ru_utime, 3),
            "cpu_sys_s": round(ru.ru_stime, 3),
            "peak_rss_bytes": peak_rss_bytes(),
            "frames": feeder.frames_submitted,
            "raw_bytes": feeder.bytes_submitted,
            "raw_mb_per_s": round(feeder.bytes_submitted / wall / 1e6, 3) if wall else None,
            "raw_mb_per_cpu_s": round(feeder.bytes_submitted / cpu / 1e6, 3) if cpu else None,
            "stopped": stop.is_set(),
            "host": describe_host(),
        }
        with feeder_stats_path(session, target.stack, repo).open("a") as f:
            f.write(json.dumps(stats) + "\n")
        ledger.close()


def _exit_with_parent(ctl: Connection) -> None:
    """The parent holds the other end of ctl until this process exits, so EOF means the parent died. A feeder must
    not outlive its ingest: it would keep sending unsupervised and hold the ledger against the rerun. It exits as
    a kill would, leaving its frames in flight in doubt."""
    with contextlib.suppress(EOFError):
        while True:
            ctl.recv()
    os._exit(1)


@dataclass
class _FeederProcess:
    """The parent's handle on one stack's feeder process: two one-way pipes and the process."""

    stack: str
    proc: BaseProcess
    ctl: Connection
    events: Connection


def run_session(
    session: Session,
    targets: list[Target],
    repo: Path = REPO,
    log: Callable[[str], None] = print,
    *,
    sample: Callable[[], None] | None = None,
    sample_every_s: float = COUNTERS_EVERY_S,
) -> dict[str, StackReport]:
    """One feeder process per stack, not a thread: frame preparation (decompress, shift, scan) holds the GIL, so
    feeder threads in one process share one core (docs/v1/units/U12.md: 4 threads, 30 MB/s together). The parent
    starts them, relays their log lines, forwards SIGINT/SIGTERM to them (each stops after its in-flight frames),
    calls `sample` (a reading of every collector's counters, record_counters) every sample_every_s and once more
    after they exit, and writes the reports from their ledgers."""
    mp = multiprocessing.get_context("spawn")
    feeders: list[_FeederProcess] = []
    for t in targets:
        ctl_r, ctl_w = mp.Pipe(duplex=False)
        ev_r, ev_w = mp.Pipe(duplex=False)
        p = mp.Process(target=feed_stack, args=(session, t, repo, ctl_r, ev_w), name=f"feeder-{t.stack}")
        p.start()
        ctl_r.close()
        ev_w.close()
        feeders.append(_FeederProcess(t.stack, p, ctl_w, ev_r))
    stopped = False

    def on_signal(signum: int, _frame: object) -> None:
        nonlocal stopped
        stopped = True
        log(f"signal {signum}: stopping feeders after in-flight frames")
        for f in feeders:
            if f.proc.is_alive() and f.proc.pid is not None:
                os.kill(f.proc.pid, signum)

    previous = {}
    if threading.current_thread() is threading.main_thread():
        previous = {s: signal_mod.signal(s, on_signal) for s in (signal_mod.SIGINT, signal_mod.SIGTERM)}
    try:
        _start(feeders, log)
        _relay(feeders, log, sample, sample_every_s)
    finally:
        for f in feeders:
            f.proc.join(30)
            if f.proc.is_alive():
                f.proc.kill()
                f.proc.join()
            f.ctl.close()
        for s, handler in previous.items():
            signal_mod.signal(s, handler)
    failed = {f.stack: f.proc.exitcode for f in feeders if f.proc.exitcode != 0}
    for stack, code in failed.items():
        log(f"{stack}: feeder process exited with code {code}")
    if sample is not None:
        sample()
    reports = write_reports(session, targets, repo)
    if not stopped and not failed and all(r.finished for r in reports.values()):
        adopt_sealed_corpus(session, repo)
        (session.dir(repo) / "ingest" / "done.json").write_text(
            json.dumps({"finished_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}) + "\n"
        )
    return reports


def _start(feeders: list[_FeederProcess], log: Callable[[str], None]) -> None:
    """Every feeder holds its ledger before any is told to send; otherwise every one is told to abort."""
    replies: dict[str, Event | None] = {}
    for f in feeders:
        try:
            replies[f.stack] = f.events.recv()
        except EOFError:
            replies[f.stack] = None
    busy = [r[1] for r in replies.values() if r is not None and r[0] == "busy"]
    dead = [s for s, r in replies.items() if r is None]
    go = not busy and not dead
    for f in feeders:
        with contextlib.suppress(OSError):
            f.ctl.send("go" if go else "abort")
    if busy:
        raise LedgerBusy(busy[0])
    if dead:
        raise RuntimeError(f"feeder process for {', '.join(dead)} exited before opening its ledger")
    for stack, r in replies.items():
        if r is not None and r[0] == "ready" and r[1]:
            log(f"{stack}: {r[1]} frame(s) in doubt from an earlier run will be resent")


def _relay(
    feeders: list[_FeederProcess], log: Callable[[str], None], sample: Callable[[], None] | None, every_s: float
) -> None:
    """Relay log lines until every feeder has closed its events pipe, sampling every every_s on the way."""
    live = {f.events: f for f in feeders}
    next_sample = time.monotonic() + every_s
    while live:
        for conn in multiprocessing.connection.wait(list(live), timeout=1.0):
            try:
                kind, value = conn.recv()  # type: ignore[union-attr]
            except EOFError:
                live.pop(conn).events.close()  # type: ignore[arg-type]
                continue
            if kind == "log":
                log(value)
        if sample is not None and time.monotonic() >= next_sample:
            sample()
            next_sample = time.monotonic() + every_s


def adopt_sealed_corpus(session: Session, repo: Path = REPO) -> Session:
    """A follow session starts before its capture seals, so it records the corpus as unsealed. Once the archive is
    sealed, the session takes the sealed corpus id (when it resumes, or when it has fed every frame), so suites,
    gates and runs built from that corpus can be matched to it. Idempotent."""
    if session.corpus_id != "unsealed":
        return session
    m = Archive.open(corpus_path(session, repo), verify="none").manifest
    if not m.sealed or m.corpus_id is None:
        return session
    sealed = replace(session, corpus_id=m.corpus_id)
    (session.dir(repo) / "session.json").write_bytes(sealed.to_json())
    return sealed


def write_reports(session: Session, targets: list[Target], repo: Path = REPO) -> dict[str, StackReport]:
    archive = Archive(corpus_path(session, repo))
    out = {}
    for t in targets:
        r = stack_report(archive, session, t.stack, t.max_late_s, session.ledger_path(t.stack, repo))
        (session.dir(repo) / "ingest" / f"{t.stack}.report.json").write_text(json.dumps(r.to_json(), indent=1) + "\n")
        out[t.stack] = r
    return out


def _summary(r: StackReport) -> dict:
    return {
        "finished": r.finished,
        "complete": r.complete,
        "rejected": len(r.rejected),
        "in_doubt": len(r.in_doubt),
        "signals": {
            s: f"{v.frames_ok}/{v.frames_expected} frames ok, {v.records_ok}/{v.records_expected} records, "
            f"{v.frames_rejected} rejected, {v.late_frames} late (bound {v.late_bound_s}), "
            f"late p50 {v.late_p50_s}s p99 {v.late_p99_s}s max {v.late_max_s}s, {v.retries} retries"
            for s, v in r.signals.items()
        },
    }


def latest_session(repo: Path = REPO) -> Session:
    root = sessions_dir(repo)
    dirs = sorted(d for d in root.iterdir() if (d / "session.json").exists()) if root.exists() else []
    if not dirs:
        raise SystemExit(f"no sessions under {root}")
    return Session.load(dirs[-1] / "session.json")


def parse_window(text: str) -> Window:
    from otb.ref.store import parse_window as pw

    return pw(text)


def counters_path(session: Session, stack: str, repo: Path = REPO) -> Path:
    """Every reading of the stack's IngestCounters in this session, one line each: at_utc, counters (per signal,
    or null when the probe failed) and error."""
    return session.dir(repo) / "ingest" / f"{stack}.counters.jsonl"


def counters_baseline(readings: Iterable[dict[str, int] | None]) -> dict[str, int]:
    """What the gate subtracts from its own reading of the collector's counters to get the records refused since
    the session started. The counters restart from zero with their collector, so a reading below the previous one
    is a reset and the increments before it are kept: the baseline is the last reading minus every increment seen,
    and goes negative after a reset. A failed reading (None: the collector was down) is no observation."""
    seen = [r for r in readings if r is not None]
    if not seen:
        return {}
    since: dict[str, int] = {}
    for prev, cur in itertools.pairwise(seen):
        for s in prev.keys() | cur.keys():
            p, c = int(prev.get(s, 0)), int(cur.get(s, 0))
            since[s] = since.get(s, 0) + (c if c < p else c - p)
    last = seen[-1]
    return {s: int(last.get(s, 0)) - since.get(s, 0) for s in sorted(last.keys() | since.keys())}


def write_counter_sample(session: Session, stack: str, reading: Probe, repo: Path = REPO) -> dict[str, int]:
    """Appends one probe result to the stack's counters file and rewrites <stack>.counters_start.json, the gate's
    baseline, from every reading so far. Returns the baseline."""
    from otb.stacks import Exact

    value = reading.value if isinstance(reading, Exact) and isinstance(reading.value, dict) else None
    path = counters_path(session, stack, repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = {
        "at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "counters": value,
        "error": None if value is not None else repr(reading),
    }
    with path.open("ab") as f:
        f.write(orjson.dumps(line) + b"\n")
    baseline = counters_baseline(x["counters"] for x in iter_jsonl(path))
    start = session.dir(repo) / "ingest" / f"{stack}.counters_start.json"
    start.write_text(json.dumps(baseline, indent=1, sort_keys=True) + "\n")
    return baseline


def record_counters(session: Session, stacks: list[StackDef], repo: Path = REPO) -> None:
    """One reading of each collector's refusal counters: when a session starts (they count from the collector's
    start, not the session's), every COUNTERS_EVERY_S while feeding, and when the feeders exit, so a collector
    restart inside the session shows as a reset in the readings and the baseline stays right."""
    from otb.stacks import IngestCounters, StackContext, run_probe

    for s in stacks:
        got = run_probe(s, IngestCounters(), None, StackContext(s, repo))  # type: ignore[arg-type]
        write_counter_sample(session, s.id, got, repo)


@command("ingest")
def cmd_ingest(argv: list[str]) -> int:
    if argv[:1] == ["status"]:
        p = argparse.ArgumentParser(prog="otb ingest status")
        p.add_argument("--session", default=None)
        a = p.parse_args(argv[1:])
        s = Session.load(sessions_dir() / a.session / "session.json") if a.session else latest_session()
        from otb.stacks import registry

        defs = registry()
        reports = write_reports(s, [Target.of(defs[x]) for x in s.stacks])
        print(
            json.dumps({"session": s.id, "shift_s": s.shift_s} | {k: _summary(v) for k, v in reports.items()}, indent=1)
        )
        return 0
    p = argparse.ArgumentParser(prog="otb ingest")
    p.add_argument("corpus", type=Path)
    p.add_argument("--mode", choices=["replay", "follow"], required=True)
    p.add_argument("--stacks", default=None, help="comma-separated, e.g. b1,b4 (default: all)")
    p.add_argument("--window", default=None, help="START/END in recording time (ns or ISO UTC): replay a slice")
    p.add_argument("--lead-s", type=int, default=60)
    p.add_argument("--allow-nonempty", action="store_true", help="skip the empty-stack check of a new replay")
    p.add_argument(
        "--wait-corpus",
        type=float,
        default=600.0,
        help="follow: seconds to wait for the capture to create the archive (start ingest with the capture)",
    )
    a = p.parse_args(argv)
    if a.mode == "follow":
        wait_for_archive(a.corpus, a.wait_corpus)
    from otb.stacks import StackContext, registry, select

    stacks = select(registry(), a.stacks)
    ids = tuple(s.id for s in stacks)
    window = parse_window(a.window) if a.window else None

    def refuse_nonempty() -> None:
        if a.mode == "replay" and not a.allow_nonempty:
            full = [s.id for s in stacks if not s.probes.is_empty(StackContext(s))]
            if full:
                sys.exit(
                    f"refusing to replay into non-empty stacks {full}: run `otb down --volumes` and `otb up` first"
                )

    session, resumed = start_session(a.corpus, ids, a.mode, window=window, lead_s=a.lead_s, before_new=refuse_nonempty)
    record_counters(session, stacks)
    first = datetime.fromtimestamp((time.time_ns() + a.lead_s * NS) / NS, UTC)
    print(
        json.dumps(
            {
                "session": session.id,
                "resumed": resumed,
                "shift_s": session.shift_s,
                "stacks": list(ids),
                "window": None if window is None else [window.start, window.end],
                "first_due_after_utc": first.strftime("%Y-%m-%dT%H:%M:%SZ") if not resumed else None,
            }
        ),
        flush=True,
    )
    try:
        reports = run_session(
            session,
            [Target.of(s) for s in stacks],
            log=lambda m: print(m, flush=True),
            sample=lambda: record_counters(session, stacks),
        )
    except LedgerBusy as e:
        sys.exit(str(e))
    print(json.dumps({k: _summary(v) for k, v in reports.items()}, indent=1))
    return 0 if all(r.finished for r in reports.values()) else 1
