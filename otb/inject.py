"""Injector: executes a plan against live flagd instances (and decoy drivers), appending every action to
events.jsonl as it happens. The realised schedule is schedule_from_events(plan, events), so a crash loses
nothing that happened and a rerun resumes: actions already confirmed are skipped, the rest run now or on time.

flagd is driven two ways, both whole-config read-modify-write with a readback:
- FlagdUi: flagd-ui's GET /api/read and POST /api/write {"data": config} (the write replaces the whole file).
- FlagdFile: the flagd JSON file mounted into the flagd container, replaced atomically; flagd polls it.
A flag is switched where flagd-ui switches it: the then-branch of a three-branch targeting rule
(productCatalogFailure's `if: [product_id == ..., "off", "off"]`, whose defaultVariant is never served), else its
defaultVariant. A write is confirmed when the config reads back with the new variant there and, when an OFREP
endpoint is given, flagd itself serves that variant for flags without targeting rules.

Each instance runs its own timeline on its own thread, so one demo's dead flag service delays and fails only that
demo's actions. An action that raises is recorded as an `error` event and the timeline goes on: the schedule holds
the entry as failed unless both of its ends were confirmed.

A release is a change of one demo Deployment's service.version, driven through a DeployDriver: a deploy fault
(relation `deploy`), whose flag is the change the release ships, or a benign_deploy decoy, which ships nothing. Its
on records the Deployment's OTEL_RESOURCE_ATTRIBUTES, sets service.version to params.version_to, waits for the
rollout and, for a fault, turns its flag on; its off turns the flag off, restores the recorded value and waits for
that rollout. Only deploy.confirm events are final, so a crash anywhere in either sequence leaves that phase to
rerun, and the recorded original is what every rollback restores. Without a DeployDriver a benign_deploy goes to
the decoy driver, as the simulated corpora's do.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Collection
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import orjson

from otb.scenario import Catalogue, Plan, PlannedEntry, RealisedSchedule, schedule_from_events, sha256_bytes
from otb.timebase import NS


class InjectError(Exception):
    pass


class Stopped(Exception):
    """Raised by the injector's sleep to end the run early: the capture is stopping."""


RETRY_S = 120.0  # a demo service restarting, or still starting, refuses connections for a while
SETTLE_EVERY_S = 60.0
RESOURCE_ENV = "OTEL_RESOURCE_ATTRIBUTES"
DEMO_LABEL, DEMO_PART_OF = "app.kubernetes.io/part-of", "opentelemetry-demo"  # the demo chart labels every workload


def _http(
    url: str,
    body: dict | None = None,
    timeout: float = 10.0,
    retry_s: float | None = None,
    form: dict[str, object] | None = None,
) -> tuple[int, bytes]:
    """One request, with a JSON body or a form body. Connection failures are retried for retry_s (a capture runs for
    days and must not end on a service restart); an HTTP error status is returned, not retried."""
    if form is not None:
        data: bytes | None = urllib.parse.urlencode(form).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
    else:
        data = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"} if data else {}
    deadline = time.monotonic() + (RETRY_S if retry_s is None else retry_s)
    delay = 1.0
    while True:
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except OSError:
            if time.monotonic() + delay > deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 15.0)


class FlagDriver(Protocol):
    def read(self) -> dict: ...
    def write(self, config: dict) -> tuple[bool, dict[str, str]]: ...
    def served_variant(self, flag: str) -> str | None: ...


def switch(flag: dict) -> tuple[dict | list, str | int]:
    """Where a flag's on/off variant lives, as flagd-ui's update_flag writes it: the then-branch of a targeting
    rule `if: [condition, then, else]`, else defaultVariant."""
    rule = (flag.get("targeting") or {}).get("if")
    return (rule, 1) if isinstance(rule, list) and len(rule) == 3 else (flag, "defaultVariant")


def flag_variant(flag: dict) -> str | None:
    where, key = switch(flag)
    return where[key] if isinstance(where, list) else where.get(key)


def ofrep_variant(ofrep_url: str | None, flag: str, retry_s: float = RETRY_S) -> str | None:
    if not ofrep_url:
        return None
    status, body = _http(f"{ofrep_url}/ofrep/v1/evaluate/flags/{flag}", {"context": {}}, retry_s=retry_s)
    return orjson.loads(body).get("variant") if status == 200 else None


@dataclass(frozen=True, slots=True)
class FlagdUi:
    url: str
    ofrep_url: str | None = None
    retry_s: float = RETRY_S

    def read(self) -> dict:
        status, body = _http(f"{self.url}/api/read", retry_s=self.retry_s)
        if status != 200:
            raise InjectError(f"GET {self.url}/api/read -> {status}")
        return orjson.loads(body)

    def write(self, config: dict) -> tuple[bool, dict[str, str]]:
        status, body = _http(f"{self.url}/api/write", {"data": config}, retry_s=self.retry_s)
        return 200 <= status < 300, {
            "http": str(status),
            "via": "flagd-ui",
            "body": body[:200].decode(errors="replace"),
        }

    def served_variant(self, flag: str) -> str | None:
        return ofrep_variant(self.ofrep_url, flag, self.retry_s)


@dataclass(frozen=True, slots=True)
class FlagdFile:
    path: Path
    ofrep_url: str | None = None

    def read(self) -> dict:
        return orjson.loads(self.path.read_bytes())

    def write(self, config: dict) -> tuple[bool, dict[str, str]]:
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        tmp.write_bytes(json.dumps(config, indent=2).encode() + b"\n")
        os.replace(tmp, self.path)
        return True, {"via": "file", "sha256": sha256_bytes(self.path.read_bytes())}

    def served_variant(self, flag: str) -> str | None:
        return ofrep_variant(self.ofrep_url, flag)


class DecoyDriver(Protocol):
    def preflight(self, entry: PlannedEntry, instance: str) -> list[str]: ...
    def start(self, entry: PlannedEntry, instance: str) -> tuple[bool, dict[str, str]]: ...
    def stop(self, entry: PlannedEntry, instance: str) -> tuple[bool, dict[str, str]]: ...
    def set_users(self, instance: str, users: int) -> tuple[bool, dict[str, str]]: ...


class DeployDriver(Protocol):
    """One demo Deployment's OTEL_RESOURCE_ATTRIBUTES (entry.target, in the instance's demo), and its rollout."""

    def resource_attributes(self, entry: PlannedEntry, instance: str) -> str: ...
    def set_resource_attributes(
        self, entry: PlannedEntry, instance: str, value: str
    ) -> tuple[bool, dict[str, str]]: ...
    def rollout(self, entry: PlannedEntry, instance: str) -> tuple[bool, dict[str, str]]: ...


def _attr_keys(items: list[str]) -> list[str]:
    return [kv.split("=", 1)[0].strip() for kv in items]


def service_version(attrs: str) -> str | None:
    items = attrs.split(",")
    keys = _attr_keys(items)
    return items[keys.index("service.version")].split("=", 1)[1] if "service.version" in keys else None


def with_service_version(attrs: str, version: str) -> str:
    """attrs with service.version set to version; every other attribute and its place kept."""
    items = [kv for kv in attrs.split(",") if kv]
    keys = _attr_keys(items)
    if "service.version" in keys:
        items[keys.index("service.version")] = f"service.version={version}"
    else:
        items.append(f"service.version={version}")
    return ",".join(items)


def locust_users(url: str, users: int) -> tuple[bool, dict[str, str]]:
    status, _ = _http(f"{url}/swarm", form={"user_count": users, "spawn_rate": users})
    return status == 200, {"http": str(status), "users": str(users), "via": "locust /swarm"}


def _locust_start(entry: PlannedEntry, url: str) -> tuple[bool, dict[str, str]] | None:
    """load_step and traffic_gap, the same on every runtime; None for a decoy type Locust does not drive."""
    match entry.decoy_type:
        case "load_step":
            return locust_users(url, int(entry.params["users_to"]))
        case "traffic_gap":
            status, _ = _http(f"{url}/stop")
            return status == 200, {"http": str(status), "via": "locust /stop"}
    return None


def _locust_stop(entry: PlannedEntry, url: str) -> tuple[bool, dict[str, str]] | None:
    if entry.decoy_type in ("traffic_gap", "load_step"):
        if "users_after" in entry.params:
            return locust_users(url, int(entry.params["users_after"]))
        return True, {"note": "nothing to undo"}
    return None


def _run(argv: list[str], env: dict[str, str] | None = None) -> tuple[bool, dict[str, str]]:
    r = subprocess.run(argv, capture_output=True, text=True, env=env)
    return r.returncode == 0, {"argv": " ".join(argv), "stdout": r.stdout[-300:], "stderr": r.stderr[-500:]}


def _read(argv: list[str]) -> str:
    r = subprocess.run(argv, capture_output=True, text=True)
    if r.returncode != 0:
        raise InjectError(r.stderr.strip() or f"{' '.join(argv)} failed")
    return r.stdout


@dataclass(frozen=True, slots=True)
class ComposeDecoys:
    """benign_restart via `docker compose restart`; load_step and traffic_gap via Locust's web API. One demo
    Compose project and Locust URL per instance, and the DOCKER_HOST of the daemon that runs it (an instance
    without one uses the injector's own environment)."""

    projects: dict[str, str]
    locust_urls: dict[str, str]
    docker_hosts: dict[str, str] = field(default_factory=dict)

    def preflight(self, entry: PlannedEntry, instance: str) -> list[str]:
        return []

    def start(self, entry: PlannedEntry, instance: str) -> tuple[bool, dict[str, str]]:
        if (done := _locust_start(entry, self.locust_urls[instance])) is not None:
            return done
        if entry.decoy_type == "benign_restart":
            host = self.docker_hosts.get(instance)
            env = os.environ | {"DOCKER_HOST": host} if host else None
            ok, detail = _run(["docker", "compose", "-p", self.projects[instance], "restart", entry.target or ""], env)
            return ok, detail | ({"docker_host": host} if host else {})
        raise InjectError(f"decoy type {entry.decoy_type} has no driver")

    def stop(self, entry: PlannedEntry, instance: str) -> tuple[bool, dict[str, str]]:
        return _locust_stop(entry, self.locust_urls[instance]) or (True, {"note": "nothing to undo"})

    def set_users(self, instance: str, users: int) -> tuple[bool, dict[str, str]]:
        return locust_users(self.locust_urls[instance], users)


@dataclass(frozen=True, slots=True)
class K8sDecoys:
    """benign_restart is a rollout restart of the target Deployment in the instance's namespace, and its end waits
    for that rollout to finish, so a restart that never completes fails its schedule entry; load_step and
    traffic_gap go to Locust's web API as on Compose. kubectl, not a Kubernetes client library: it resolves a
    kubeconfig context or the in-cluster service account the way every other tool here does, and `rollout restart`
    is exactly the patch kubectl documents (a restartedAt annotation on the pod template), with no new dependency.

    The restart event is written when the patch is accepted, before the new pod starts, so the window opens before
    the restarted process's first telemetry. flagd is never a target: its flag file lives in the pod, and a new
    pod reverts every flag the injector wrote.

    As a DeployDriver it changes one env var of the container named after its Deployment with `kubectl set env`,
    which converges when repeated. Every change first reads the Deployment and refuses one outside the instance's
    namespace or without the demo chart's label, so it acts on nothing but the demo it was given."""

    namespaces: dict[str, str]
    locust_urls: dict[str, str]
    context: str | None = None
    rollout_timeout_s: int = 180

    def _kubectl(self, *args: str) -> list[str]:
        return ["kubectl", *(["--context", self.context] if self.context else []), *args]

    def _demo_container(self, entry: PlannedEntry, instance: str) -> tuple[str, dict]:
        """(namespace, container) of entry.target in the instance's demo namespace; InjectError for anything else."""
        ns = self.namespaces.get(instance)
        if ns is None:
            raise InjectError(f"no demo namespace for instance {instance}")
        name = entry.target or ""
        if name == "flagd":
            raise InjectError("changing flagd would revert the injected flags")
        d = orjson.loads(_read(self._kubectl("get", f"deployment/{name}", "-n", ns, "-o", "json")))
        meta = d.get("metadata") or {}
        part_of = (meta.get("labels") or {}).get(DEMO_LABEL)
        if meta.get("namespace") != ns or part_of != DEMO_PART_OF:
            raise InjectError(
                f"deployment/{name} in {meta.get('namespace')} is not an OpenTelemetry Demo Deployment"
                f" ({DEMO_LABEL} is {part_of!r}, not {DEMO_PART_OF!r}); refusing to change it"
            )
        container = next((c for c in d["spec"]["template"]["spec"]["containers"] if c["name"] == name), None)
        if container is None:
            raise InjectError(f"deployment/{name} in {ns} has no container {name}")
        return ns, container

    def resource_attributes(self, entry: PlannedEntry, instance: str) -> str:
        _, container = self._demo_container(entry, instance)
        value = next((e.get("value") for e in container.get("env") or () if e["name"] == RESOURCE_ENV), None)
        if value is None:
            raise InjectError(f"deployment/{entry.target} sets no {RESOURCE_ENV}, so a release cannot set its version")
        return value

    def set_resource_attributes(self, entry: PlannedEntry, instance: str, value: str) -> tuple[bool, dict[str, str]]:
        ns, container = self._demo_container(entry, instance)
        argv = [
            "set",
            "env",
            f"deployment/{entry.target}",
            "-n",
            ns,
            "-c",
            container["name"],
            f"{RESOURCE_ENV}={value}",
        ]
        return _run(self._kubectl(*argv))

    def rollout(self, entry: PlannedEntry, instance: str) -> tuple[bool, dict[str, str]]:
        ns, wait = self.namespaces[instance], f"--timeout={self.rollout_timeout_s}s"
        return _run(self._kubectl("rollout", "status", f"deployment/{entry.target}", "-n", ns, wait))

    def preflight(self, entry: PlannedEntry, instance: str) -> list[str]:
        if entry.decoy_type != "benign_restart":
            return []
        if instance not in self.namespaces:
            return [f"{entry.slot_id}: no namespace for instance {instance}"]
        if entry.target == "flagd":
            return [f"{entry.slot_id}: restarting flagd would revert the injected flags"]
        ns = self.namespaces[instance]
        ok, detail = _run(self._kubectl("get", f"deployment/{entry.target}", "-n", ns, "-o", "name"))
        return [] if ok else [f"{entry.slot_id}: {instance}: {detail['stderr'].strip() or detail['argv'] + ' failed'}"]

    def start(self, entry: PlannedEntry, instance: str) -> tuple[bool, dict[str, str]]:
        if (done := _locust_start(entry, self.locust_urls[instance])) is not None:
            return done
        if entry.decoy_type == "benign_restart":
            if entry.target == "flagd":
                raise InjectError("restarting flagd would revert the injected flags")
            ns = self.namespaces[instance]
            return _run(self._kubectl("rollout", "restart", f"deployment/{entry.target}", "-n", ns))
        raise InjectError(f"decoy type {entry.decoy_type} has no driver")

    def stop(self, entry: PlannedEntry, instance: str) -> tuple[bool, dict[str, str]]:
        if (done := _locust_stop(entry, self.locust_urls[instance])) is not None:
            return done
        if entry.decoy_type == "benign_restart":
            return self.rollout(entry, instance)
        return True, {"note": "nothing to undo"}

    def set_users(self, instance: str, users: int) -> tuple[bool, dict[str, str]]:
        return locust_users(self.locust_urls[instance], users)


def read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = path.read_bytes()
    return [orjson.loads(line) for line in data[: data.rfind(b"\n") + 1].splitlines()]


@dataclass(frozen=True, slots=True)
class _Step:
    offset_s: int
    phase: str
    entry: PlannedEntry
    instance: str


class Injector:
    def __init__(
        self,
        plan: Plan,
        catalogue: Catalogue,
        flags: dict[str, FlagDriver],
        events_path: Path,
        capture_start_ns: int,
        *,
        decoys: DecoyDriver | None = None,
        deploys: DeployDriver | None = None,
        confirm_timeout_s: float = 15.0,
        clock: Callable[[], int] = time.time_ns,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.plan = plan
        self.catalogue = catalogue
        self.flags = flags
        self.events_path = events_path
        self.capture_start_ns = capture_start_ns
        self.decoys = decoys
        self.deploys = deploys
        self.confirm_timeout_s = confirm_timeout_s
        self.clock = clock
        self.sleep = sleep
        self._events_lock = threading.Lock()

    def preflight(self) -> list[str]:
        """Every planned flag and variant exists in each targeted instance's live config, every catalogued flag
        is off, every planned decoy has a driver, and every release's Deployment is the demo's and not already on
        the release's version."""
        problems = []
        for inst, driver in sorted(self.flags.items()):
            cfg = driver.read()
            problems += [f"{inst}: {p}" for p in self.catalogue.check_against_flagd(orjson.dumps(cfg), pinned=False)]
            for flag in sorted(self.catalogue.faults):
                v = flag_variant(cfg["flags"].get(flag, {}))
                if v is not None and v != self.catalogue.off_variant:
                    problems.append(f"{inst}: {flag} is {v!r}, not off, before the capture")
        for e in self.plan.entries:
            for inst in e.instances:
                if e.kind == "fault" and inst not in self.flags:
                    problems.append(f"{e.slot_id}: no flagd driver for instance {inst}")
                if e.relation == "deploy" and self.deploys is None:
                    problems.append(f"{e.slot_id}: a deploy fault needs --decoy-driver k8s")
                elif self._is_release(e):
                    problems += self._preflight_release(e, inst)
                if e.kind in ("decoy", "load") and self.decoys is None:
                    problems.append(f"{e.slot_id}: a {e.kind} needs a decoy driver")
                elif e.kind == "decoy" and self.decoys is not None:
                    problems += self.decoys.preflight(e, inst)
        return problems

    def _is_release(self, e: PlannedEntry) -> bool:
        return e.relation == "deploy" or (e.decoy_type == "benign_deploy" and self.deploys is not None)

    def _preflight_release(self, e: PlannedEntry, inst: str) -> list[str]:
        assert self.deploys is not None
        version = e.params.get("version_to")
        if not version:
            return [f"{e.slot_id}: a release needs params.version_to"]
        try:
            attrs = self.deploys.resource_attributes(e, inst)
        except InjectError as x:
            return [f"{e.slot_id}: {inst}: {x}"]
        if service_version(attrs) == version:
            return [f"{e.slot_id}: {inst}: deployment/{e.target} already runs service.version {version}"]
        return []

    def run(self) -> RealisedSchedule:
        """Resumable: confirmed actions are skipped. A fault, decoy or quiet window that ended before its start ran
        (a restart after a long outage) is recorded as skipped, so it is failed in the schedule rather than
        realised for an instant. A quiet window needs no action: its on and off are marks that the injector was
        running then, which is what realises it. Each instance's timeline runs on its own thread and ends by
        turning that instance's flags off. Stopped, raised by the sleep, ends every timeline and is raised here."""
        done = {(e["slot_id"], e["instance"], e["phase"]) for e in read_events(self.events_path) if e.get("final")}
        steps: dict[str, list[_Step]] = {inst: [] for inst in self.flags}
        for step in self._timeline():
            steps.setdefault(step.instance, []).append(step)
        with ThreadPoolExecutor(max(1, len(steps)), thread_name_prefix="otb-inject") as pool:
            runs = [
                pool.submit(self._run_instance, inst, todo, {k for k in done if k[1] == inst})
                for inst, todo in sorted(steps.items())
            ]
        for r in runs:
            r.result()
        return self.schedule()

    def schedule(self) -> RealisedSchedule:
        plan_sha = sha256_bytes(self.plan.to_yaml())
        return schedule_from_events(self.plan, plan_sha, self.capture_start_ns, read_events(self.events_path))

    def _run_instance(self, inst: str, steps: list[_Step], done: set[tuple[str, str, str]]) -> None:
        """One instance's timeline. After an action of this instance fails, its flags are settled before each later
        action and every SETTLE_EVERY_S, so a fault whose off failed is turned off once the flag service answers."""
        closed: set[str] = set()
        dirty = False
        try:
            for step in steps:
                key = (step.entry.slot_id, step.instance)
                if (*key, step.phase) in done:
                    continue
                end_ns = self.capture_start_ns + step.entry.end_offset_s * NS
                if step.phase == "on" and step.entry.kind != "load" and self.clock() >= end_ns:
                    # a release's off still runs: a Deployment changed before a crash must be rolled back
                    for phase in ("on",) if self._is_release(step.entry) else ("on", "off"):
                        detail = {"reason": "the window ended before the injector reached it"}
                        self._event(key[0], key[1], "skip", phase, False, detail, final=True)
                        done.add((*key, phase))
                    continue
                if dirty:
                    dirty = not self._try(inst, None, "settle", lambda: self._settle(inst, closed.__contains__))
                target = self.capture_start_ns + step.offset_s * NS
                wait = (target - self.clock()) / NS
                while wait > 0 and dirty:
                    self.sleep(min(wait, SETTLE_EVERY_S))
                    dirty = not self._try(inst, None, "settle", lambda: self._settle(inst, closed.__contains__))
                    wait = (target - self.clock()) / NS
                if wait > 0:
                    self.sleep(wait)
                ok = self._try(inst, step.entry.slot_id, step.phase, lambda s=step: self._act(s))
                if step.phase == "off":
                    closed.add(step.entry.slot_id)
                dirty = dirty or not ok
        finally:
            self._try(inst, None, "reset", lambda: self._settle(inst, lambda _: True))

    def _try(self, inst: str, slot: str | None, phase: str, action: Callable[[], bool]) -> bool:
        """Run one action. Any exception but Stopped is recorded as an `error` event, and the action counts as
        failed."""
        try:
            return action()
        except Stopped:
            raise
        except Exception as e:
            print(f"otb inject: {inst} {slot or '-'} {phase} failed: {e!r}", file=sys.stderr, flush=True)
            try:
                self._event(slot, inst, "error", phase, False, {"error": repr(e)[:500]})
            except OSError as w:
                print(f"otb inject: could not record that failure: {w!r}", file=sys.stderr, flush=True)
            return False

    def _settle(self, inst: str, closing: Callable[[str], bool]) -> bool:
        """Turn off every catalogued flag on inst that no open fault holds. A flag held by a fault confirmed on is
        turned off as that fault's off once closing(slot) says its off is due, so the schedule records the window
        the fault really had and a resumed run does not turn it off again. Any other flag that is on is reset.
        True when every such flag was confirmed off.

        A release whose off is due is rolled back here. An open bad deploy whose off is not due holds its flag, which
        neither the reset nor another release's rollback turns off. Stopped during one rollback does not skip the
        others or the flag reset: it is raised once they are done."""
        ok, stopped = True, False
        releases = self._open_releases(inst)
        held = {e.flag for e in releases if e.kind == "fault" and not closing(e.slot_id)}
        for e in releases:
            if closing(e.slot_id):
                try:
                    ok &= self._try(inst, e.slot_id, "off", lambda e=e: self._roll_back(e, inst, held))
                except Stopped:
                    ok, stopped = False, True
        driver = self.flags.get(inst)
        if driver is None:
            if stopped:
                raise Stopped
            return ok
        off = self.catalogue.off_variant
        flag_of = {e.slot_id: e.flag for e in self.plan.entries if e.kind == "fault" and e.relation != "deploy"}
        opened: dict[str, str] = {}
        for e in read_events(self.events_path):
            if e["instance"] == inst and e["action"] == "flag.confirm" and e.get("final") and e["slot_id"] in flag_of:
                flag = flag_of[e["slot_id"]]
                if e["phase"] == "on":
                    opened[flag] = e["slot_id"]
                elif opened.get(flag) == e["slot_id"]:
                    del opened[flag]
        cfg = driver.read()
        for flag in sorted(self.catalogue.faults):
            if flag_variant(cfg["flags"].get(flag, {})) in (None, off):
                continue
            slot = opened.get(flag)
            if slot is None and flag in held:
                continue
            if slot is None:
                ok &= self._set_flag(driver, inst, None, "reset", flag, off)
            elif closing(slot):
                ok &= self._set_flag(driver, inst, slot, "off", flag, off)
        if stopped:
            raise Stopped
        return ok

    def _timeline(self) -> list[_Step]:
        steps = []
        for e in self.plan.entries:
            for inst in e.instances:
                steps.append(_Step(e.start_offset_s, "on", e, inst))
                if e.kind != "load":
                    steps.append(_Step(e.end_offset_s, "off", e, inst))
        return sorted(steps, key=lambda s: (s.offset_s, s.phase != "off", s.entry.slot_id, s.instance))

    def _act(self, step: _Step) -> bool:
        e = step.entry
        if self._is_release(e):
            return (self._release if step.phase == "on" else self._roll_back)(e, step.instance)
        if e.kind == "fault":
            variant = e.variant if step.phase == "on" else self.catalogue.off_variant
            return self._set_flag(self.flags[step.instance], step.instance, e.slot_id, step.phase, e.flag, variant)
        if e.kind == "quiet":
            self._event(e.slot_id, step.instance, "quiet.mark", step.phase, True, {}, final=True)
            return True
        assert self.decoys is not None
        if e.kind == "load":
            ok, detail = self.decoys.set_users(step.instance, int(e.params["users"]))
            self._event(e.slot_id, step.instance, "load.set", step.phase, ok, detail, final=True)
            return ok
        ok, detail = (self.decoys.start if step.phase == "on" else self.decoys.stop)(e, step.instance)
        self._event(e.slot_id, step.instance, f"decoy.{e.decoy_type}", step.phase, ok, detail, final=True)
        return ok

    def _original(self, slot: str, inst: str) -> str | None:
        """The resource attributes recorded before the release first changed anything: what a rollback restores."""
        return next(
            (
                ev["detail"]["original"]
                for ev in read_events(self.events_path)
                if ev["slot_id"] == slot and ev["instance"] == inst and ev["action"] == "deploy.read"
            ),
            None,
        )

    def _open_releases(self, inst: str) -> list[PlannedEntry]:
        """Releases on inst that may have changed their Deployment and are not confirmed rolled back."""
        began, closed = set(), set()
        for ev in read_events(self.events_path):
            if ev["instance"] != inst:
                continue
            if ev["action"] == "deploy.read":
                began.add(ev["slot_id"])
            elif ev["action"] == "deploy.confirm" and ev["phase"] == "off" and ev.get("final"):
                closed.add(ev["slot_id"])
        open_ = began - closed
        return [e for e in self.plan.entries if self._is_release(e) and inst in e.instances and e.slot_id in open_]

    def _release(self, e: PlannedEntry, inst: str) -> bool:
        """A release's on. The original value is recorded once, before the first change, so a rerun after a crash
        at any later point restores it rather than the release. A bad deploy's flag goes on only once the rollout is
        done, so no old-version pod serves a slow request. A release that does not get that far is rolled back at
        once."""
        assert self.deploys is not None
        version = e.params["version_to"]
        current = self.deploys.resource_attributes(e, inst)
        original = self._original(e.slot_id, inst)
        if original is None:
            if service_version(current) == version:
                raise InjectError(f"deployment/{e.target} already runs {version} and no original value is recorded")
            original = current
            self._event(e.slot_id, inst, "deploy.read", "on", True, {"original": original})
        ok, detail = self.deploys.set_resource_attributes(e, inst, with_service_version(original, version))
        self._event(e.slot_id, inst, "deploy.apply", "on", ok, detail)
        ready = False
        if ok:
            ready, detail = self.deploys.rollout(e, inst)
            self._event(e.slot_id, inst, "deploy.rollout", "on", ready, detail)
        ok = ready
        if ready and e.kind == "fault":
            ok = self._set_flag(self.flags[inst], inst, e.slot_id, "on", e.flag or "", e.variant or "", final=False)
        if not ok:
            self._undo(e, inst, "on", flag=ready and e.kind == "fault")
        release = {"deployment": e.target, "from_version": service_version(original), "to_version": version}
        if e.kind == "fault":
            release |= {"flag": e.flag, "variant": e.variant}
        self._event(e.slot_id, inst, "deploy.confirm", "on", ok, release, final=ok)
        return ok

    def _roll_back(self, e: PlannedEntry, inst: str, held: Collection[str | None] = ()) -> bool:
        """A release's off: back to the recorded original, confirmed once its rollout is done. A release that never
        changed anything has nothing to undo but a bad deploy's flag, and a flag another open release holds is that
        release's to turn off. A release whose Deployment another change has replaced has nothing left to undo: it
        is closed as failed, so it is never retried over the change that replaced it."""
        undone = self._undo(e, inst, "off", flag=e.kind == "fault" and e.flag not in held)
        restored = {"deployment": e.target, "to_version": service_version(self._original(e.slot_id, inst) or "")}
        if undone is None:
            restored["note"] = "superseded: another change replaced this release before its rollback"
        self._event(e.slot_id, inst, "deploy.confirm", "off", bool(undone), restored, final=undone is not False)
        return bool(undone)

    def _undo(self, e: PlannedEntry, inst: str, phase: str, *, flag: bool) -> bool | None:
        """The flag off first, so the restored version never serves a request it slows, then the original value and
        its rollout. Each step converges when repeated. The Deployment is restored even when the flag step fails or
        the capture is stopping, since a demo service that cannot reach flagd gets the default it asks for, off
        (shipping asks for 0). It is restored only while it still carries this release, so a late rollback never
        undoes a later one: it returns None then."""
        assert self.deploys is not None
        flag_ok, stopped = True, False
        if flag:
            try:
                flag_ok = self._set_flag(
                    self.flags[inst], inst, e.slot_id, phase, e.flag or "", self.catalogue.off_variant, final=False
                )
            except Stopped:
                flag_ok, stopped = False, True
            except Exception as x:
                self._event(e.slot_id, inst, "error", phase, False, {"error": repr(x)[:500]})
                flag_ok = False
        ok = True
        original = self._original(e.slot_id, inst)
        if original is not None:
            current = self.deploys.resource_attributes(e, inst)
            released = with_service_version(original, e.params["version_to"])
            if current == released:
                done, detail = self.deploys.set_resource_attributes(e, inst, original)
                self._event(e.slot_id, inst, "deploy.apply", phase, done, detail)
                ok = done
            elif current != original:
                note = {"note": f"left at service.version {service_version(current)}: another change replaced it"}
                self._event(e.slot_id, inst, "deploy.apply", phase, True, note)
                if stopped:
                    raise Stopped
                return None
            if ok:
                ready, detail = self.deploys.rollout(e, inst)
                self._event(e.slot_id, inst, "deploy.rollout", phase, ready, detail)
                ok = ready
        if stopped:
            raise Stopped
        return flag_ok and ok

    def _set_flag(
        self,
        driver: FlagDriver,
        inst: str,
        slot: str | None,
        phase: str,
        flag: str,
        variant: str,
        *,
        final: bool = True,
    ) -> bool:
        """Write the flag and wait for its readback. The confirm closes its phase (final) when it confirmed and
        `final`: a deploy fault's flag is one step of its phase, never the last."""
        cfg = driver.read()
        if flag not in cfg["flags"] or variant not in cfg["flags"][flag]["variants"]:
            self._event(slot, inst, "flag.write", phase, False, {"flag": flag, "variant": variant, "error": "unknown"})
            return False
        where, key = switch(cfg["flags"][flag])
        where[key] = variant
        ok, detail = driver.write(cfg)
        self._event(slot, inst, "flag.write", phase, ok, detail | {"flag": flag, "variant": variant})
        if not ok:
            return False
        targeted = "targeting" in cfg["flags"][flag]
        t0 = time.monotonic()
        seen: tuple[str | None, str | None] = (None, None)
        while time.monotonic() - t0 < self.confirm_timeout_s:
            readback = flag_variant(driver.read()["flags"][flag])
            served = None if targeted else driver.served_variant(flag)
            seen = (readback, served)
            if readback == variant and (served is None or served == variant):
                break
            self.sleep(0.2)
        readback, served = seen
        confirmed = readback == variant and (served is None or served == variant)
        detail = {"flag": flag, "variant": variant, "readback": str(readback), "served": str(served)}
        detail["confirm_s"] = f"{time.monotonic() - t0:.3f}"
        self._event(slot, inst, "flag.confirm", phase, confirmed, detail, final=confirmed and final)
        return confirmed

    def _event(
        self, slot: str | None, inst: str, action: str, phase: str, ok: bool, detail: dict, *, final: bool = False
    ) -> None:
        with self._events_lock, self.events_path.open("ab") as f:
            line = {"at_ns": self.clock(), "slot_id": slot, "instance": inst, "action": action, "phase": phase}
            line |= {"ok": ok, "final": final, "detail": {k: str(v) for k, v in detail.items()}}
            f.write(orjson.dumps(line) + b"\n")
            f.flush()
            os.fsync(f.fileno())
