"""The capture gateway: an OTLP/HTTP JSON receiver that writes every export request into the corpus archive.

The OTel Demo's collector exports to it with `encoding: json`, a file-backed sending queue and unlimited retry
(capture/demo-overlay/). A request is normalised, appended to the archive and fsynced before the 200 goes back,
so an acked batch survives a crash and an un-acked one is resent by the collector. A resent batch the archive
already holds is dropped by body sha (ArchiveWriter's dedupe, bounded by count and age) and still acked.

Normalise and append run on one worker thread: the writer is single-threaded by contract, and the event loop
stays free to accept connections and answer /health. More than `max_pending` requests in flight get 503 with
Retry-After, which the collector retries, so back-pressure lands in its on-disk queue, not in gateway memory.
A storage error is fatal: the gateway answers 503 and exits, and the restart's archive recovery truncates
whatever the failed append left behind.

The collector's file queue outlives its container, so a new capture can be sent batches an earlier one left
unsent. A request whose newest event precedes the archive's creation by more than stale_s is quarantined: acked,
recorded in quarantine.jsonl and the manifest, and not archived.
"""

from __future__ import annotations

import asyncio
import os
import resource
import signal
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import orjson
import yaml
from aiohttp import web

from otb.archive import SIGNALS, ArchiveWriter, Header, Manifest, NormaliserStamp, Signal, SourcePins
from otb.host import describe as describe_host
from otb.host import peak_rss_bytes
from otb.inject import DecoyDriver, DeployDriver, FlagDriver, Injector, Stopped
from otb.normalise import normalise
from otb.scenario import Catalogue, load_scenario, plan, sha256_bytes
from otb.timebase import NS

PATHS: dict[str, Signal] = {"/v1/traces": "traces", "/v1/logs": "logs", "/v1/metrics": "metrics"}
TOP_KEY: dict[Signal, bytes] = {
    "traces": b'{"resourceSpans":',
    "logs": b'{"resourceLogs":',
    "metrics": b'{"resourceMetrics":',
}
MAX_BODY = 256 << 20
STALE_S = 600.0
PROTOBUF_HINT = (
    "this gateway accepts OTLP/HTTP JSON only; set `encoding: json` on the collector's otlp_http exporter"
    " (see capture/demo-overlay/otelcol-otb.yml)"
)


class BadRequest(Exception):
    pass


@dataclass(slots=True)
class SignalStats:
    requests: int = 0
    frames: int = 0
    records: int = 0
    wire_bytes: int = 0
    raw_bytes: int = 0
    stored_bytes: int = 0
    dedupe_hits: int = 0
    quarantined: int = 0


@dataclass(slots=True)
class Stats:
    started_ns: int = field(default_factory=time.time_ns)
    first_request_ns: int = 0
    last_ack_ns: int = 0
    signals: dict[str, SignalStats] = field(default_factory=lambda: {s: SignalStats() for s in SIGNALS})
    rejects: Counter = field(default_factory=Counter)


class Gateway:
    def __init__(
        self, writer: ArchiveWriter, *, max_pending: int = 64, report_every_s: float = 5.0, stale_s: float = STALE_S
    ) -> None:
        self.writer = writer
        self.stale_s = stale_s
        self.stale_before_ns = writer.created_ns - stale_s * NS
        self.max_pending = max_pending
        self.report_every_s = report_every_s
        self.stats = Stats()
        self.failed: BaseException | None = None
        self.stop_requested = asyncio.Event()
        started = datetime.fromtimestamp(self.stats.started_ns / 1e9, UTC).strftime("%Y%m%dT%H%M%S")
        self.report_path = writer.root / "derived" / "gateway" / f"{writer.stream}-{started}-{os.getpid()}.json"
        self._pending = 0
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="otb-archive")
        self._runner: web.AppRunner | None = None
        self._reporter: asyncio.Task | None = None

    def app(self) -> web.Application:
        app = web.Application(client_max_size=MAX_BODY)
        for path in PATHS:
            app.router.add_post(path, self._ingest)
        app.router.add_get("/health", self._health)
        return app

    async def start(self, host: str, port: int) -> None:
        self._runner = web.AppRunner(self.app(), handle_signals=False, access_log=None, shutdown_timeout=30)
        await self._runner.setup()
        await web.TCPSite(self._runner, host, port, reuse_address=True).start()
        self._reporter = asyncio.create_task(self._report_loop())

    async def stop(self) -> None:
        """Stop accepting, let in-flight requests finish, seal the open segments, write the final report."""
        if self._runner is not None:
            await self._runner.cleanup()
        if self._reporter is not None:
            self._reporter.cancel()
        await asyncio.get_running_loop().run_in_executor(None, self._pool.shutdown, True)
        if self.failed is None:
            self.writer.seal_segments()
        self.write_report()

    async def _ingest(self, request: web.Request) -> web.Response:
        signal = PATHS[request.path]
        st = self.stats.signals[signal]
        st.requests += 1
        if not self.stats.first_request_ns:
            self.stats.first_request_ns = time.time_ns()
        if request.content_type != "application/json":
            return self._reject(signal, 415, f"Content-Type {request.content_type!r}: {PROTOBUF_HINT}")
        if self.failed is not None or self._pending >= self.max_pending:
            self.stats.rejects[f"{signal} 503 busy"] += 1
            return web.Response(status=503, headers={"Retry-After": "1"}, text="gateway busy, retry")
        self._pending += 1
        try:
            raw = await request.read()
            st.wire_bytes += request.content_length or len(raw)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(self._pool, self._store, signal, raw, time.time_ns())
        except BadRequest as e:
            return self._reject(signal, 400, str(e))
        except OSError as e:
            self.failed = e
            self.stop_requested.set()
            print(f"otb gateway: storage error, stopping: {e!r}", file=sys.stderr, flush=True)
            self.stats.rejects[f"{signal} 503 storage"] += 1
            return web.Response(status=503, headers={"Retry-After": "5"}, text="storage error")
        finally:
            self._pending -= 1
        self.stats.last_ack_ns = time.time_ns()
        return web.Response(body=b"{}", content_type="application/json")

    def _store(self, signal: Signal, raw: bytes, received_ns: int) -> None:
        try:
            n = normalise(signal, raw)
        except (ValueError, TypeError, AttributeError) as e:
            raise BadRequest(f"not an OTLP/JSON {signal} export request: {e}") from e
        if n.body != b"{}" and not n.body.startswith(TOP_KEY[signal]):
            raise BadRequest(f"not an OTLP/JSON {signal} export request: top-level key is not {TOP_KEY[signal][2:-2]}")
        st = self.stats.signals[signal]
        if n.records and 0 < n.max_event_ns < self.stale_before_ns:
            age_s = (self.writer.created_ns - n.max_event_ns) // NS
            reason = f"newest event {age_s} s before the archive was created (bound {self.stale_s:g} s)"
            self.writer.quarantine(signal, n, received_ns, reason)
            st.raw_bytes += len(raw)
            st.quarantined += 1
            return
        entry = self.writer.append(signal, n, received_ns)
        st.raw_bytes += len(raw)
        if entry is None:
            st.dedupe_hits += 1
            return
        st.frames += 1
        st.records += n.records
        st.stored_bytes += entry.length

    def _reject(self, signal: Signal, status: int, message: str) -> web.Response:
        self.stats.rejects[f"{signal} {status}"] += 1
        return web.Response(status=status, text=message)

    async def _health(self, _: web.Request) -> web.Response:
        body = {"status": "failed" if self.failed else "ok", "pending": self._pending} | self.report()
        return web.Response(body=orjson.dumps(body), content_type="application/json")

    async def _report_loop(self) -> None:
        while True:
            await asyncio.sleep(self.report_every_s)
            self.write_report()

    def report(self) -> dict:
        s = self.stats
        ru = resource.getrusage(resource.RUSAGE_SELF)
        totals = SignalStats(*(sum(getattr(x, f) for x in s.signals.values()) for f in SignalStats.__slots__))
        active_s = (s.last_ack_ns - s.first_request_ns) / 1e9 if s.last_ack_ns > s.first_request_ns else 0.0
        return {
            "stream": self.writer.stream,
            "pid": os.getpid(),
            "started_utc": datetime.fromtimestamp(s.started_ns / 1e9, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "uptime_s": round((time.time_ns() - s.started_ns) / 1e9, 3),
            "active_s": round(active_s, 3),
            "signals": {k: asdict(v) for k, v in s.signals.items()},
            "totals": asdict(totals),
            "raw_mb_per_s_active": round(totals.raw_bytes / 1e6 / active_s, 2) if active_s else None,
            "frames_per_s_active": round(totals.frames / active_s, 1) if active_s else None,
            "rejects": dict(sorted(s.rejects.items())),
            "peak_rss_mb": round(peak_rss_bytes() / 2**20, 1),
            "cpu_user_s": round(ru.ru_utime, 3),
            "cpu_sys_s": round(ru.ru_stime, 3),
            "failed": repr(self.failed) if self.failed else None,
            "host": describe_host(),
        }

    def write_report(self) -> None:
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.report_path.with_name(self.report_path.name + ".tmp")
        tmp.write_bytes(orjson.dumps(self.report(), option=orjson.OPT_INDENT_2) + b"\n")
        os.replace(tmp, self.report_path)


def parse_listen(listen: str) -> tuple[str, int]:
    host, _, port = listen.rpartition(":")
    return host or "0.0.0.0", int(port)


async def wait_set(event: asyncio.Event, timeout_s: float | None) -> bool:
    """Whether the event was set within timeout_s."""
    try:
        await asyncio.wait_for(event.wait(), timeout_s)
    except TimeoutError:
        return False
    return True


def install_stop_signals(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)


async def serve(
    writer: ArchiveWriter, listen: str, *, duration_s: float | None = None, stale_s: float = STALE_S
) -> Gateway:
    """Run until SIGINT/SIGTERM, a storage error or duration_s, then stop gracefully."""
    gw = Gateway(writer, stale_s=stale_s)
    install_stop_signals(gw.stop_requested)
    host, port = parse_listen(listen)
    await gw.start(host, port)
    print(f"otb gateway: listening on {host}:{port}, writing {writer.root} stream {writer.stream}", flush=True)
    await wait_set(gw.stop_requested, duration_s)
    await gw.stop()
    return gw


DEMO_REPO = "https://github.com/open-telemetry/opentelemetry-demo"
UNRECORDED = "unrecorded (pass --source)"


class CaptureError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class CaptureState:
    """out/capture.json, written once when a capture first starts; a rerun into the same directory resumes it."""

    scenario_sha256: str
    seed: int
    plan_sha256: str
    capture_start_ns: int
    duration_s: int
    instances: tuple[str, ...]


def source_pins(path: Path | None) -> SourcePins:
    if path is None:
        return SourcePins(DEMO_REPO, UNRECORDED, UNRECORDED, {}, UNRECORDED)
    d = yaml.safe_load(path.read_text())
    return SourcePins(
        demo_repo=d.get("demo_repo", DEMO_REPO),
        demo_commit=d["demo_commit"],
        demo_tag=d["demo_tag"],
        service_images=dict(d.get("service_images") or {}),
        demo_collector_image=d["demo_collector_image"],
    )


def open_writer(
    out: Path, instances: tuple[str, ...], *, stream: str, rotate_bytes: int, source: Path | None
) -> ArchiveWriter:
    header = Header(out.name, instances, source_pins(source), NormaliserStamp.current(), rotate_bytes)
    return ArchiveWriter(out, header, stream=stream)


def gateway_only(
    out: Path,
    *,
    listen: str,
    stream: str = "g0",
    rotate_bytes: int = 64 << 20,
    instances: tuple[str, ...] = ("default",),
    duration_s: float | None = None,
    seal: bool = True,
    source: Path | None = None,
    stale_s: float = STALE_S,
) -> Manifest | None:
    """`otb capture --gateway-only`: archive whatever the demo sends until stopped. With seal, a clean stop seals
    the archive with no scenario and no schedule (injection was by hand); without, a rerun appends to it."""
    writer = open_writer(out, instances, stream=stream, rotate_bytes=rotate_bytes, source=source)
    try:
        if writer.sealed:
            print(f"otb capture: {out} is already sealed", flush=True)
            return writer.sealed_manifest
        gw = asyncio.run(serve(writer, listen, duration_s=duration_s, stale_s=stale_s))
        if gw.failed is not None or not seal:
            return None
        return writer.seal(scenario_sha256=None, schedule_sha256=None)
    finally:
        writer.close()


def _atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _state(out: Path, want: CaptureState) -> CaptureState:
    path = out / "capture.json"
    if not path.exists():
        _atomic(path, orjson.dumps(asdict(want), option=orjson.OPT_INDENT_2) + b"\n")
        return want
    d = orjson.loads(path.read_bytes())
    have = CaptureState(**(d | {"instances": tuple(d["instances"])}))
    same = (have.scenario_sha256, have.seed, have.duration_s, have.instances)
    if same != (want.scenario_sha256, want.seed, want.duration_s, want.instances):
        raise CaptureError(f"{path} is a capture of a different scenario, seed, duration or instance set")
    return have


def capture(
    scenario_path: Path,
    out: Path,
    flags: dict[str, FlagDriver],
    *,
    duration_s: int | None = None,
    seed: int | None = None,
    listen: str = "0.0.0.0:4318",
    stream: str = "g0",
    rotate_bytes: int = 64 << 20,
    drain_s: float = 30.0,
    decoys: DecoyDriver | None = None,
    deploys: DeployDriver | None = None,
    source: Path | None = None,
    wait_demo_s: float = 600.0,
    stale_s: float = STALE_S,
    before_start: Callable[[], None] | None = None,
    after: Callable[[], None] | None = None,
) -> Manifest | None:
    """`otb capture`: run the gateway and the injector on the scenario's plan until capture start + duration,
    keep the gateway up for drain_s so the collector's queue empties, then seal with the realised schedule.
    `before_start` runs once the gateway listens and before the capture start is fixed (the background
    generators go up here, so their first batch has a gateway to reach); `after` runs when the gateway is down.

    Resumable and idempotent. The capture start, seed and duration are fixed in capture.json on the first run; a
    rerun into the same directory appends to the archive and resumes the injector from events.jsonl against the
    original clock. A rerun of a sealed capture returns its manifest. SIGINT/SIGTERM or a storage error stops
    early without sealing (flags are turned off and the faults' windows end there), and returns None. The
    injector never stops the gateway: a failed action is recorded in events.jsonl and fails its schedule entry."""
    sc = load_scenario(scenario_path)
    cat = Catalogue.load()
    seed = sc.seed if seed is None else seed
    if seed is None:
        raise CaptureError("the scenario has no seed; pass --seed")
    if sorted(flags) != sorted(sc.instances):
        raise CaptureError(f"flagd drivers for {sorted(flags)} but the scenario's instances are {list(sc.instances)}")
    p = plan(sc, cat, seed)
    plan_yaml = p.to_yaml()
    duration_s = duration_s or sc.duration_s
    writer = open_writer(out, sc.instances, stream=stream, rotate_bytes=rotate_bytes, source=source)
    try:
        if writer.sealed:
            print(f"otb capture: {out} is already sealed", flush=True)
            return writer.sealed_manifest
        stop_inject = threading.Event()

        def sleep(s: float) -> None:
            if stop_inject.wait(s):
                raise Stopped

        events = out / "events.jsonl"

        def start() -> tuple[Injector, int]:
            """Runs once the gateway listens, so a demo started alongside the capture never queues telemetry: wait
            for every instance's flag service, check the plan against it, then fix the capture's start."""
            if before_start is not None:
                before_start()
            if not (out / "capture.json").exists():
                problems = preflight_when_reachable(
                    Injector(p, cat, flags, events, 0, decoys=decoys, deploys=deploys), wait_demo_s
                )
                if problems:
                    raise CaptureError("preflight failed:\n  " + "\n  ".join(problems))
            want = CaptureState(sc.sha256, seed, sha256_bytes(plan_yaml), time.time_ns(), duration_s, sc.instances)
            state = _state(out, want)
            for name, data in (("scenario.yaml", scenario_path.read_bytes()), ("plan.yaml", plan_yaml)):
                if not (out / name).exists():
                    _atomic(out / name, data)
            injector = Injector(
                p, cat, flags, events, state.capture_start_ns, decoys=decoys, deploys=deploys, sleep=sleep
            )
            return injector, state.capture_start_ns + duration_s * NS

        try:
            result = asyncio.run(_run_capture(writer, listen, start, stop_inject, drain_s, stale_s))
        finally:
            if after is not None:
                after()
        if result is None:
            return None
        injector = result
        schedule_yaml = injector.schedule().to_yaml()
        _atomic(out / "schedule.yaml", schedule_yaml)
        return writer.seal(scenario_sha256=sc.sha256, schedule_sha256=sha256_bytes(schedule_yaml))
    finally:
        writer.close()


def preflight_when_reachable(injector: Injector, wait_s: float, poll_s: float = 5.0) -> list[str]:
    """The injector's preflight, retried while the demo's flag service refuses connections (it is still starting),
    for at most wait_s. Any other problem is returned at once."""
    deadline = time.monotonic() + wait_s
    while True:
        try:
            return injector.preflight()
        except OSError as e:
            if time.monotonic() >= deadline:
                return [f"flag service unreachable for {wait_s:g} s: {e}"]
            print(f"otb capture: waiting for the demo's flag service ({e})", flush=True)
            time.sleep(poll_s)


async def _run_capture(
    writer: ArchiveWriter,
    listen: str,
    start: Callable[[], tuple[Injector, int]],
    stop_inject: threading.Event,
    drain_s: float,
    stale_s: float,
) -> Injector | None:
    gw = Gateway(writer, stale_s=stale_s)
    install_stop_signals(gw.stop_requested)
    await gw.start(*parse_listen(listen))
    print(f"otb capture: gateway on {listen}", flush=True)
    try:
        injector, end_ns = await asyncio.to_thread(start)
    except BaseException:
        await gw.stop()
        raise
    print(f"otb capture: the capture ends at {end_ns} ns", flush=True)

    def inject() -> None:
        """The injector records each failed action and goes on; whatever still escapes it ends injection only."""
        try:
            injector.run()
        except Stopped:
            pass
        except Exception as e:
            print(f"otb capture: injector stopped, the gateway goes on: {e!r}", file=sys.stderr, flush=True)

    injecting = asyncio.create_task(asyncio.to_thread(inject))
    completed = not await wait_set(gw.stop_requested, max(0.0, (end_ns - time.time_ns()) / 1e9))
    stop_inject.set()
    await injecting
    if completed:
        print(f"otb capture: duration reached; draining for {drain_s:g} s", flush=True)
        await wait_set(gw.stop_requested, drain_s)
    await gw.stop()
    completed = completed and gw.failed is None
    if not completed:
        print("otb capture: stopped before the end; rerun the same command to resume", flush=True)
    return injector if completed else None
