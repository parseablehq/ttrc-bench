"""Corpus archive, format otb-archive/1: the single source of data and the ingestion bus.

  segments/<signal>/<stream>/<seq:06d>.otlp.zst       concatenated independent zstd frames, one per normalised batch
  segments/<signal>/<stream>/<seq:06d>.otlp.zst.open  the segment being written; renamed on seal
  segments/<signal>/<stream>/<seq:06d>.idx.jsonl      one FrameEntry per frame, written after the frame
  segments.jsonl                                      one SegmentEntry per sealed segment, append-only
  manifest.yaml                                       header; corpus_id set once sealed (corpus_id_of)
  quarantine.jsonl                                    requests acked but not archived (Quarantined), append-only

A frame's bytes are fsynced before its index line, and a reader trusts only indexed frames, so it never sees a
torn frame, live or after a crash. Sealed segments never change. Everything under derived/ is outside corpus_id.
"""

from __future__ import annotations

import fcntl
import hashlib
import heapq
import os
import time
from collections import deque
from collections.abc import Callable, Iterator
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import orjson
import yaml
import zstandard

from otb.normalise import RULES, VERSION, Normalised, normalise, source_sha256
from otb.timebase import NS, EventNs, Window

Signal = Literal["traces", "logs", "metrics"]
SIGNALS: tuple[Signal, ...] = ("traces", "logs", "metrics")
FORMAT = "otb-archive/1"
ZSTD_LEVEL = 3
OPEN = ".open"


class ArchiveError(Exception):
    pass


@dataclass(frozen=True, slots=True, order=True)
class FrameRef:
    """Storage address of one batch. Ordering is storage order, not event order."""

    signal: Signal
    stream: str
    seq: int
    index: int


@dataclass(frozen=True, slots=True)
class FrameEntry:
    index: int
    offset: int
    length: int
    records: int
    min_event_ns: int
    max_event_ns: int
    body_sha256: str
    received_ns: int
    dropped: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SegmentEntry:
    signal: Signal
    stream: str
    seq: int
    path: str
    bytes: int
    sha256: str
    index_sha256: str
    frames: int
    records: int
    min_event_ns: int
    max_event_ns: int

    @property
    def index_path(self) -> str:
        return self.path.removesuffix(".otlp.zst") + ".idx.jsonl"


@dataclass(frozen=True, slots=True)
class SourcePins:
    demo_repo: str
    demo_commit: str
    demo_tag: str
    service_images: dict[str, str]
    demo_collector_image: str


@dataclass(frozen=True, slots=True)
class NormaliserStamp:
    version: str
    source_sha256: str
    rules: tuple[str, ...]

    @staticmethod
    def current() -> NormaliserStamp:
        return NormaliserStamp(VERSION, source_sha256(), tuple(RULES.values()))


@dataclass(frozen=True, slots=True)
class SignalTotals:
    segments: int
    frames: int
    records: int
    bytes: int


@dataclass(frozen=True, slots=True)
class Header:
    """What the writer is told up front; the rest of the manifest is computed from the segments."""

    name: str
    instances: tuple[str, ...]
    source: SourcePins
    normaliser: NormaliserStamp
    rotate_bytes: int
    imported_from: dict | None = None


@dataclass(frozen=True, slots=True)
class Manifest:
    format: str
    name: str
    corpus_id: str | None
    sealed: bool
    created_utc: str
    window: Window | None
    instances: tuple[str, ...]
    streams: tuple[str, ...]
    source: SourcePins
    normaliser: NormaliserStamp
    normaliser_dropped: dict[str, int]
    rotate_bytes: int
    scenario_sha256: str | None
    schedule_sha256: str | None
    totals: dict[str, SignalTotals]
    imported_from: dict | None = None
    quarantined: dict[str, dict[str, int]] = field(default_factory=dict)  # signal -> frames, records

    def to_yaml(self) -> str:
        d = {
            "format": self.format,
            "name": self.name,
            "corpus_id": self.corpus_id,
            "sealed": self.sealed,
            "created_utc": self.created_utc,
            "window": None if self.window is None else {"start": self.window.start, "end": self.window.end},
            "instances": list(self.instances),
            "streams": list(self.streams),
            "source": asdict(self.source),
            "normaliser": asdict(self.normaliser) | {"rules": list(self.normaliser.rules)},
            "normaliser_dropped": self.normaliser_dropped,
            "rotate_bytes": self.rotate_bytes,
            "scenario_sha256": self.scenario_sha256,
            "schedule_sha256": self.schedule_sha256,
            "totals": {s: asdict(t) for s, t in self.totals.items()},
        }
        if self.imported_from is not None:
            d["imported_from"] = self.imported_from
        if self.quarantined:
            d["quarantined"] = self.quarantined
        return yaml.safe_dump(d, sort_keys=False, width=120)

    @staticmethod
    def from_yaml(text: str) -> Manifest:
        d = yaml.safe_load(text)
        if d.get("format") != FORMAT:
            raise ArchiveError(f"not an {FORMAT} manifest: format={d.get('format')!r}")
        w = d["window"]
        n = d["normaliser"]
        return Manifest(
            format=d["format"],
            name=d["name"],
            corpus_id=d["corpus_id"],
            sealed=bool(d["sealed"]),
            created_utc=d["created_utc"],
            window=None if w is None else Window(EventNs(w["start"]), EventNs(w["end"])),
            instances=tuple(d["instances"]),
            streams=tuple(d["streams"]),
            source=SourcePins(**d["source"]),
            normaliser=NormaliserStamp(n["version"], n["source_sha256"], tuple(n["rules"])),
            normaliser_dropped=dict(d["normaliser_dropped"]),
            rotate_bytes=int(d["rotate_bytes"]),
            scenario_sha256=d["scenario_sha256"],
            schedule_sha256=d["schedule_sha256"],
            totals={s: SignalTotals(**t) for s, t in d["totals"].items()},
            imported_from=d.get("imported_from"),
            quarantined={s: dict(c) for s, c in (d.get("quarantined") or {}).items()},
        )


@dataclass(frozen=True, slots=True)
class Quarantined:
    """One request the gateway acknowledged but did not archive, because its events belong to an earlier capture."""

    signal: str
    stream: str
    records: int
    min_event_ns: int
    max_event_ns: int
    body_sha256: str
    received_ns: int
    reason: str


def _seg_rel(signal: str, stream: str, seq: int) -> str:
    return f"segments/{signal}/{stream}/{seq:06d}.otlp.zst"


def _jsonl(obj: object) -> bytes:
    return orjson.dumps(asdict(obj)) + b"\n"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _read_index(path: Path) -> list[FrameEntry]:
    """Complete lines only: a torn last line from a crash is not a frame yet."""
    if not path.exists():
        return []
    data = path.read_bytes()
    end = data.rfind(b"\n") + 1
    return [FrameEntry(**orjson.loads(line)) for line in data[:end].splitlines()]


def _read_segments(root: Path) -> list[SegmentEntry]:
    path = root / "segments.jsonl"
    if not path.exists():
        return []
    return [SegmentEntry(**orjson.loads(line)) for line in path.read_bytes().splitlines() if line]


def corpus_id_of(segments: list[SegmentEntry]) -> str:
    """sha256 of the segment entries sorted by (signal, stream, seq): a function of content, not of the order in
    which concurrent stream writers appended to segments.jsonl."""
    ordered = sorted(segments, key=lambda s: (s.signal, s.stream, s.seq))
    return hashlib.sha256(b"".join(_jsonl(s) for s in ordered)).hexdigest()


def _now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(slots=True)
class _OpenSegment:
    seq: int
    data: object
    index: object
    size: int
    frames: int = 0
    records: int = 0
    min_ns: int = 0
    max_ns: int = 0
    opened_at: float = field(default_factory=time.monotonic)

    def account(self, e: FrameEntry) -> None:
        self.frames += 1
        self.records += e.records
        if e.min_event_ns:
            self.min_ns = e.min_event_ns if not self.min_ns else min(self.min_ns, e.min_event_ns)
        self.max_ns = max(self.max_ns, e.max_event_ns)


class Dedupe:
    """Body shas of recent frames, bounded by count and by age: a client retry of a stored batch is dropped."""

    def __init__(self, max_count: int = 10_000, max_age_s: int = 600) -> None:
        self.max_count = max_count
        self.max_age_ns = max_age_s * NS
        self._recent: deque[tuple[int, str]] = deque()
        self._shas: dict[str, int] = {}

    def seen(self, sha: str, now_ns: int) -> bool:
        while self._recent and (len(self._recent) > self.max_count or now_ns - self._recent[0][0] > self.max_age_ns):
            _, old = self._recent.popleft()
            if self._shas.get(old, 0) <= 1:
                self._shas.pop(old, None)
            else:
                self._shas[old] -= 1
        return sha in self._shas

    def add(self, sha: str, now_ns: int) -> None:
        self._recent.append((now_ns, sha))
        self._shas[sha] = self._shas.get(sha, 0) + 1


class ArchiveWriter:
    """Single writer of one stream, enforced by a lock file. Reopening an unsealed archive recovers it: bytes past
    the last indexed frame are truncated, a torn index line is dropped, and a segment renamed but not yet listed
    in segments.jsonl is listed. Appending then continues exactly where an uninterrupted writer would be."""

    def __init__(
        self,
        root: Path,
        header: Header,
        *,
        stream: str = "g0",
        rotate_s: int | None = 600,
        dedupe: bool = True,
    ) -> None:
        self.root = root
        self.header = header
        self.stream = stream
        self.rotate_s = rotate_s
        self.dedupe: dict[Signal, Dedupe | None] = {s: Dedupe() if dedupe else None for s in SIGNALS}
        self._cctx = zstandard.ZstdCompressor(level=ZSTD_LEVEL)
        root.mkdir(parents=True, exist_ok=True)
        self._lock = (root / ".writer.lock").open("w")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise ArchiveError(f"another writer holds {root / '.writer.lock'}") from e
        self._open: dict[Signal, _OpenSegment | None] = dict.fromkeys(SIGNALS)
        self._next_seq: dict[Signal, int] = dict.fromkeys(SIGNALS, 0)
        self._sealed_frames: dict[Signal, int] = dict.fromkeys(SIGNALS, 0)
        manifest_path = root / "manifest.yaml"
        if manifest_path.exists():
            m = Manifest.from_yaml(manifest_path.read_text())
            self.sealed = m.sealed
            if m.normaliser.version != header.normaliser.version or m.rotate_bytes != header.rotate_bytes:
                raise ArchiveError(f"{root} was started with a different normaliser or rotation; not appending")
        else:
            m = self._manifest(segments=[], sealed=False)
            self.sealed = False
            _atomic_write(manifest_path, m.to_yaml().encode())
        # The manifest is rewritten only at the seal, so until then its created_utc is when the archive was created.
        self.created_ns = (
            int(datetime.strptime(m.created_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()) * NS
        )
        if not self.sealed:
            self._recover()

    @property
    def sealed_manifest(self) -> Manifest:
        return Manifest.from_yaml((self.root / "manifest.yaml").read_text())

    def frames_written(self, signal: Signal) -> int:
        o = self._open[signal]
        return self._sealed_frames[signal] + (o.frames if o else 0)

    def last_frame(self, signal: Signal) -> FrameEntry | None:
        o = self._open[signal]
        if o and o.frames:
            return _read_index(self._dir(signal) / f"{o.seq:06d}.idx.jsonl")[-1]
        segs = [s for s in _read_segments(self.root) if s.signal == signal and s.stream == self.stream]
        return _read_index(self.root / segs[-1].index_path)[-1] if segs else None

    def append(self, signal: Signal, n: Normalised, received_ns: int) -> FrameEntry | None:
        if self.sealed:
            raise ArchiveError(f"{self.root} is sealed")
        sha = hashlib.sha256(n.body).hexdigest()
        dd = self.dedupe[signal]
        if dd is not None and dd.seen(sha, received_ns):
            return None
        o = self._open[signal] or self._start(signal)
        frame = self._cctx.compress(n.body)
        o.data.write(frame)
        o.data.flush()
        os.fsync(o.data.fileno())
        e = FrameEntry(
            o.frames, o.size, len(frame), n.records, n.min_event_ns, n.max_event_ns, sha, received_ns, n.dropped
        )
        o.index.write(_jsonl(e))
        o.index.flush()
        os.fsync(o.index.fileno())
        o.size += len(frame)
        o.account(e)
        if dd is not None:
            dd.add(sha, received_ns)
        if o.size >= self.header.rotate_bytes or (
            self.rotate_s is not None and time.monotonic() - o.opened_at >= self.rotate_s
        ):
            self._seal_segment(signal)
        return e

    def quarantine(self, signal: Signal, n: Normalised, received_ns: int, reason: str) -> Quarantined:
        """Record a request that is acknowledged but not archived; fsynced before the caller acks it."""
        if self.sealed:
            raise ArchiveError(f"{self.root} is sealed")
        q = Quarantined(
            signal,
            self.stream,
            n.records,
            n.min_event_ns,
            n.max_event_ns,
            hashlib.sha256(n.body).hexdigest(),
            received_ns,
            reason,
        )
        with (self.root / "quarantine.jsonl").open("ab") as f:
            f.write(_jsonl(q))
            f.flush()
            os.fsync(f.fileno())
        return q

    def seal(self, *, scenario_sha256: str | None, schedule_sha256: str | None) -> Manifest:
        """Seal every open segment and write the final manifest. Sealing a sealed archive returns its manifest."""
        if self.sealed:
            return self.sealed_manifest
        self.seal_segments()
        m = self._manifest(_read_segments(self.root), sealed=True)
        m = replace(m, scenario_sha256=scenario_sha256, schedule_sha256=schedule_sha256)
        _atomic_write(self.root / "manifest.yaml", m.to_yaml().encode())
        self.sealed = True
        return m

    def seal_segments(self) -> list[SegmentEntry]:
        """Seal every open segment, leaving the archive open for its schedule to be written; returns all
        segments. Idempotent."""
        for s in SIGNALS:
            o = self._open[s]
            if o is None:
                continue
            if o.frames:
                self._seal_segment(s)
            else:
                o.data.close()
                o.index.close()
                (self._dir(s) / f"{o.seq:06d}.otlp.zst{OPEN}").unlink()
                (self._dir(s) / f"{o.seq:06d}.idx.jsonl").unlink()
                self._open[s] = None
        return _read_segments(self.root)

    def close(self) -> None:
        for o in self._open.values():
            if o is not None:
                o.data.close()
                o.index.close()
        self._lock.close()

    def _dir(self, signal: Signal) -> Path:
        return self.root / "segments" / signal / self.stream

    def _start(self, signal: Signal) -> _OpenSegment:
        d = self._dir(signal)
        d.mkdir(parents=True, exist_ok=True)
        seq = self._next_seq[signal]
        o = _OpenSegment(
            seq=seq,
            data=(d / f"{seq:06d}.otlp.zst{OPEN}").open("ab"),
            index=(d / f"{seq:06d}.idx.jsonl").open("ab"),
            size=0,
        )
        _fsync_dir(d)
        self._open[signal] = o
        return o

    def _seal_segment(self, signal: Signal) -> None:
        o = self._open[signal]
        assert o is not None
        o.data.close()
        o.index.close()
        d = self._dir(signal)
        os.replace(d / f"{o.seq:06d}.otlp.zst{OPEN}", d / f"{o.seq:06d}.otlp.zst")
        _fsync_dir(d)
        self._list_segment(signal, o.seq)
        self._open[signal] = None

    def _list_segment(self, signal: Signal, seq: int) -> None:
        rel = _seg_rel(signal, self.stream, seq)
        path = self.root / rel
        entries = _read_index(path.with_name(f"{seq:06d}.idx.jsonl"))
        nonzero = [e.min_event_ns for e in entries if e.min_event_ns]
        seg = SegmentEntry(
            signal=signal,
            stream=self.stream,
            seq=seq,
            path=rel,
            bytes=path.stat().st_size,
            sha256=_sha256_file(path),
            index_sha256=_sha256_file(path.with_name(f"{seq:06d}.idx.jsonl")),
            frames=len(entries),
            records=sum(e.records for e in entries),
            min_event_ns=min(nonzero, default=0),
            max_event_ns=max((e.max_event_ns for e in entries), default=0),
        )
        with (self.root / "segments.jsonl").open("ab") as f:
            f.write(_jsonl(seg))
            f.flush()
            os.fsync(f.fileno())
        self._sealed_frames[signal] += seg.frames
        self._next_seq[signal] = seq + 1

    def _recover(self) -> None:
        listed = {(s.signal, s.seq) for s in _read_segments(self.root) if s.stream == self.stream}
        for s in _read_segments(self.root):
            if s.stream == self.stream:
                self._sealed_frames[s.signal] += s.frames
                self._next_seq[s.signal] = max(self._next_seq[s.signal], s.seq + 1)
        for signal in SIGNALS:
            d = self._dir(signal)
            if not d.exists():
                continue
            for p in sorted(d.glob("*.otlp.zst")):
                seq = int(p.name[:6])
                if (signal, seq) not in listed:
                    self._list_segment(signal, seq)
            for p in sorted(d.glob(f"*.otlp.zst{OPEN}")):
                seq = int(p.name[:6])
                idx = d / f"{seq:06d}.idx.jsonl"
                entries = _read_index(idx)
                end = entries[-1].offset + entries[-1].length if entries else 0
                with p.open("r+b") as f:
                    f.truncate(end)
                    os.fsync(f.fileno())
                with idx.open("r+b") as f:
                    f.truncate(sum(len(_jsonl(e)) for e in entries))
                    os.fsync(f.fileno())
                o = _OpenSegment(seq=seq, data=p.open("ab"), index=idx.open("ab"), size=end)
                for e in entries:
                    o.account(e)
                self._open[signal] = o
                self._next_seq[signal] = seq + 1
        self._seed_dedupe()

    def _seed_dedupe(self) -> None:
        """Refill each dedupe window from the newest indexed frames, sealed segments included: a batch acked just
        before a crash can sit in a segment that rotated before the crash, and its retry must still be dropped."""
        segs = [s for s in _read_segments(self.root) if s.stream == self.stream]
        for signal in SIGNALS:
            dd = self.dedupe[signal]
            if dd is None:
                continue
            o = self._open[signal]
            recent = _read_index(self._dir(signal) / f"{o.seq:06d}.idx.jsonl") if o else []
            for seg in reversed([s for s in segs if s.signal == signal]):
                if len(recent) >= dd.max_count:
                    break
                recent = _read_index(self.root / seg.index_path) + recent
            for e in recent[-dd.max_count :]:
                dd.add(e.body_sha256, e.received_ns)

    def _manifest(self, segments: list[SegmentEntry], *, sealed: bool) -> Manifest:
        h = self.header
        totals: dict[str, SignalTotals] = {}
        dropped = dict.fromkeys(RULES, 0)
        for s in SIGNALS:
            segs = [x for x in segments if x.signal == s]
            totals[s] = SignalTotals(
                len(segs), sum(x.frames for x in segs), sum(x.records for x in segs), sum(x.bytes for x in segs)
            )
        for x in segments:
            for e in _read_index(self.root / x.index_path):
                for k, v in e.dropped.items():
                    dropped[k] += v
        mins = [x.min_event_ns for x in segments if x.min_event_ns]
        maxs = [x.max_event_ns for x in segments]
        window = None
        if mins:
            minute = 60 * NS
            window = Window(EventNs(min(mins) // minute * minute), EventNs(-(-max(maxs) // minute) * minute))
        corpus_id = corpus_id_of(segments) if sealed else None
        quarantined: dict[str, dict[str, int]] = {}
        path = self.root / "quarantine.jsonl"
        data = path.read_bytes() if path.exists() else b""
        for line in data[: data.rfind(b"\n") + 1].splitlines():
            q = orjson.loads(line)
            c = quarantined.setdefault(q["signal"], {"frames": 0, "records": 0})
            c["frames"] += 1
            c["records"] += q["records"]
        return Manifest(
            format=FORMAT,
            name=h.name,
            corpus_id=corpus_id,
            sealed=sealed,
            created_utc=_now_utc(),
            window=window,
            instances=h.instances,
            streams=tuple(sorted({x.stream for x in segments} | {self.stream})),
            source=h.source,
            normaliser=h.normaliser,
            normaliser_dropped=dropped,
            rotate_bytes=h.rotate_bytes,
            scenario_sha256=None,
            schedule_sha256=None,
            totals=totals,
            imported_from=h.imported_from,
            quarantined=dict(sorted(quarantined.items())),
        )


class _Tail:
    """Incremental reader of one (signal, stream): returns newly indexed frames, moving to the next segment once
    the current one is sealed and fully consumed."""

    def __init__(self, root: Path, signal: Signal, stream: str) -> None:
        self.root = root
        self.signal = signal
        self.stream = stream
        self.seq = 0
        self.pos = 0
        self.index = 0

    def _dir(self) -> Path:
        return self.root / "segments" / self.signal / self.stream

    def poll(self) -> list[tuple[FrameRef, FrameEntry]]:
        out: list[tuple[FrameRef, FrameEntry]] = []
        while True:
            idx = self._dir() / f"{self.seq:06d}.idx.jsonl"
            if idx.exists():
                with idx.open("rb") as f:
                    f.seek(self.pos)
                    data = f.read()
                end = data.rfind(b"\n") + 1
                for line in data[:end].splitlines():
                    e = FrameEntry(**orjson.loads(line))
                    out.append((FrameRef(self.signal, self.stream, self.seq, e.index), e))
                    self.index = e.index + 1
                self.pos += end
            sealed = (self._dir() / f"{self.seq:06d}.otlp.zst").exists()
            if not sealed:
                return out
            sealed_frames = len(_read_index(idx))
            if self.index < sealed_frames:
                continue
            self.seq += 1
            self.pos = 0
            self.index = 0


def _merge_key(item: tuple[FrameRef, FrameEntry]) -> tuple:
    ref, e = item
    return (e.max_event_ns, ref.signal, ref.stream, ref.seq, ref.index)


class Archive:
    """Read side, used by feeders, the reference build and verification."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._dctx = zstandard.ZstdDecompressor()
        self._index_cache: dict[tuple[str, str, int], list[FrameEntry]] = {}

    @staticmethod
    def open(root: Path, *, verify: Literal["none", "index", "full"] = "index", workers: int = 4) -> Archive:
        a = Archive(root)
        if verify != "none":
            problems = a.verify(full=verify == "full", workers=workers)
            if problems:
                raise ArchiveError(f"{root}: " + "; ".join(problems[:10]))
        return a

    @property
    def manifest(self) -> Manifest:
        return Manifest.from_yaml((self.root / "manifest.yaml").read_text())

    def segments(self, signal: Signal | None = None) -> list[SegmentEntry]:
        return [s for s in _read_segments(self.root) if signal is None or s.signal == signal]

    def streams(self) -> list[tuple[Signal, str]]:
        found = set()
        for s in SIGNALS:
            d = self.root / "segments" / s
            if d.exists():
                found |= {(s, p.name) for p in d.iterdir() if p.is_dir()}
        return sorted(found)

    def index(self, signal: Signal, stream: str, seq: int) -> list[FrameEntry]:
        key = (signal, stream, seq)
        if key in self._index_cache:
            return self._index_cache[key]
        rel = _seg_rel(signal, stream, seq)
        entries = _read_index(self.root / (rel.removesuffix(".otlp.zst") + ".idx.jsonl"))
        if (self.root / rel).exists():
            self._index_cache[key] = entries
        return entries

    def frames(
        self, signal: Signal, stream: str, *, after: FrameRef | None = None, follow: bool = False, poll_s: float = 0.5
    ) -> Iterator[tuple[FrameRef, FrameEntry]]:
        """Indexed frames in storage order. With follow, tails the open segment until the archive is sealed and
        every frame has been yielded."""
        tail = _Tail(self.root, signal, stream)
        while True:
            sealed_before = follow and self.manifest.sealed
            got = tail.poll()
            for item in got:
                if after is None or item[0] > after:
                    yield item
            if not follow or (sealed_before and not got):
                return
            if not got:
                time.sleep(poll_s)

    def merged(
        self,
        *,
        skip: Callable[[FrameRef], bool] | None = None,
        follow: bool = False,
        hold_s: float = 5.0,
        poll_s: float = 0.5,
    ) -> Iterator[tuple[FrameRef, FrameEntry]]:
        """Every frame, ordered by (max_event_ns, signal, stream, seq, index): the due order of a replay, total, so
        two replays send frames in the same order. Without follow this is a global sort with bounded memory: a
        frame inside a stream may arrive later than frames due after it, so segments are opened in order of their
        earliest due time and only segments whose due ranges overlap are held at once.
        With follow (a live archive), streams are merged in arrival order as frames are indexed; a stream with
        nothing new holds the merge for at most hold_s, so a quiet signal cannot stall the others."""
        if not follow:
            for item in self._sorted():
                if skip is None or not skip(item[0]):
                    yield item
            return
        tails: dict[tuple[Signal, str], _Tail] = {}
        pending: dict[tuple[Signal, str], deque] = {}
        blocked_since: float | None = None
        while True:
            sealed = self.manifest.sealed
            for key in self.streams():
                if key not in tails:
                    tails[key] = _Tail(self.root, *key)
                    pending[key] = deque()
                pending[key].extend(i for i in tails[key].poll() if skip is None or not skip(i[0]))
            heads = [q[0] for q in pending.values() if q]
            if not heads:
                if sealed:
                    return
                time.sleep(poll_s)
                continue
            empty = [k for k, q in pending.items() if not q]
            if empty and not sealed:
                now = time.monotonic()
                blocked_since = blocked_since or now
                if now - blocked_since < hold_s:
                    time.sleep(poll_s)
                    continue
            blocked_since = None
            item = min(heads, key=_merge_key)
            pending[(item[0].signal, item[0].stream)].popleft()
            yield item

    def _sorted(self) -> Iterator[tuple[FrameRef, FrameEntry]]:
        """Global due order of a sealed archive. A segment's earliest due time is a lower bound for every frame in
        it, so once the heap's head is due before the next unopened segment's lower bound, nothing unopened can
        precede it."""
        bounds = []
        for seg in self.segments():
            entries = _read_index(self.root / seg.index_path)
            dues = [e.max_event_ns for e in entries if e.records]
            if dues:
                bounds.append((min(dues), seg.signal, seg.stream, seg.seq))
        bounds.sort()
        heap: list[tuple[tuple, FrameRef, FrameEntry]] = []
        nxt = 0
        while heap or nxt < len(bounds):
            while nxt < len(bounds) and (not heap or bounds[nxt][0] <= heap[0][0][0]):
                _, signal, stream, seq = bounds[nxt]
                rel = _seg_rel(signal, stream, seq).removesuffix(".otlp.zst") + ".idx.jsonl"
                for e in _read_index(self.root / rel):
                    ref = FrameRef(signal, stream, seq, e.index)
                    heapq.heappush(heap, (_merge_key((ref, e)), ref, e))
                nxt += 1
            _, ref, e = heapq.heappop(heap)
            yield ref, e

    def read(self, ref: FrameRef, entry: FrameEntry | None = None) -> bytes:
        e = entry or self.index(ref.signal, ref.stream, ref.seq)[ref.index]
        path = self.root / _seg_rel(ref.signal, ref.stream, ref.seq)
        if not path.exists():
            path = path.with_name(path.name + OPEN)
        with path.open("rb") as f:
            f.seek(e.offset)
            return self._dctx.decompress(f.read(e.length))

    def bodies(self, seg: SegmentEntry) -> Iterator[tuple[FrameEntry, bytes]]:
        """Every frame of a sealed segment, decompressed, reading the file sequentially."""
        entries = _read_index(self.root / seg.index_path)
        with (self.root / seg.path).open("rb") as f:
            for e in entries:
                f.seek(e.offset)
                yield e, self._dctx.decompress(f.read(e.length))

    def verify(self, *, full: bool, workers: int = 4) -> list[str]:
        """'index' level: corpus_id, totals and every index file's sha and the segment sizes. 'full' also re-hashes
        every segment and checks each frame: body sha, record count, and that normalise() leaves it unchanged."""
        problems: list[str] = []
        m = self.manifest
        segs = self.segments()
        if not m.sealed:
            problems.append("archive is not sealed")
        else:
            got = corpus_id_of(segs)
            if got != m.corpus_id:
                problems.append(f"corpus_id {m.corpus_id} != corpus_id_of(segments.jsonl) {got}")
        for s in SIGNALS:
            ss = [x for x in segs if x.signal == s]
            want = m.totals.get(s)
            have = SignalTotals(
                len(ss), sum(x.frames for x in ss), sum(x.records for x in ss), sum(x.bytes for x in ss)
            )
            if want != have:
                problems.append(f"{s}: manifest totals {want} != segments {have}")
        for seg in segs:
            problems += _verify_index(self.root, seg)
        if full and segs:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                for found in pool.map(_verify_full, [self.root] * len(segs), segs):
                    problems += found
        return problems


def _verify_index(root: Path, seg: SegmentEntry) -> list[str]:
    idx = root / seg.index_path
    out = []
    if not (root / seg.path).exists():
        return [f"{seg.path}: missing"]
    if (root / seg.path).stat().st_size != seg.bytes:
        out.append(f"{seg.path}: size {(root / seg.path).stat().st_size} != {seg.bytes}")
    if not idx.exists() or _sha256_file(idx) != seg.index_sha256:
        out.append(f"{seg.index_path}: sha mismatch")
    return out


def _verify_full(root: Path, seg: SegmentEntry) -> list[str]:
    out = []
    if _sha256_file(root / seg.path) != seg.sha256:
        out.append(f"{seg.path}: sha mismatch")
    a = Archive(root)
    entries = _read_index(root / seg.index_path)
    expect_offset = 0
    records = 0
    for e, body in a.bodies(seg):
        where = f"{seg.path}#{e.index}"
        if e.offset != expect_offset:
            out.append(f"{where}: offset {e.offset} != {expect_offset}")
        expect_offset = e.offset + e.length
        if hashlib.sha256(body).hexdigest() != e.body_sha256:
            out.append(f"{where}: body sha mismatch")
            continue
        n = normalise(seg.signal, body)
        if n.body != body:
            out.append(f"{where}: body is not a normaliser fixed point")
        if n.records != e.records:
            out.append(f"{where}: records {n.records} != index {e.records}")
        records += n.records
    if expect_offset != seg.bytes:
        out.append(f"{seg.path}: frames end at {expect_offset}, file is {seg.bytes} bytes")
    if len(entries) != seg.frames or records != seg.records:
        out.append(f"{seg.path}: {len(entries)} frames/{records} records != entry {seg.frames}/{seg.records}")
    return out
