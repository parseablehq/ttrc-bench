"""Fault catalogue, scenarios with placeholder slots, the pure planner, and the realised schedule.

Each fact has one owner:
- scenarios/faults.yaml: what each flagd flag does (class, root service, variants, symptom rule). Authored, pinned
  to the demo's flagd file by sha256.
- scenario.yaml: what may happen. Slots name candidate flags and ranges; `{{fault.*}}` is bound by the plan. A
  fault slot with `relation: deploy` is a bad release: its flag's root service is rolled to params.version_to and
  the flag is the change the release ships. Its class is bad_deploy; the flag's own class is accepted beside it.
- plan.yaml: plan(scenario, catalogue, seed). Pure; anyone can recompute it byte for byte.
- events.jsonl: what the injector did, appended as it happens.
- schedule.yaml: the realised schedule, a pure function of the plan and events.jsonl. Never edited afterwards.
- derived/schedule_check.json: observed onset, offset and signature from the reference store (ref/schedule_check).
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Literal

import orjson
import yaml

from otb.timebase import NS, EventNs, Window, render_utc

REPO = Path(__file__).resolve().parents[1]
DEFAULT_CATALOGUE = REPO / "scenarios" / "faults.yaml"

FaultClass = Literal[
    "error_injection",
    "dependency_unreachable",
    "latency_injection",
    "cpu_saturation",
    "memory_leak",
    "gc_pressure",
    "queue_backlog",
    "cache_failure",
    "lock_contention",
    "traffic_flood",
    "agent_loop",
    "readiness_failure",
]
DecoyType = Literal["load_step", "traffic_gap", "benign_restart", "benign_deploy", "noisy_logger"]
SlotKind = Literal["fault", "decoy", "quiet"]


class ScenarioError(Exception):
    pass


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _dump(obj: object) -> bytes:
    return yaml.safe_dump(obj, sort_keys=False, width=120, allow_unicode=True).encode()


# ---------- fault catalogue ----------


@dataclass(frozen=True, slots=True)
class Symptom:
    """Either a reference measure with its params, or `unverified` with the reason no rule exists yet."""

    measure: str | None
    params: dict[str, object]
    unverified: str | None = None


@dataclass(frozen=True, slots=True)
class FaultKind:
    flag: str
    fault_class: FaultClass
    root_service: str
    variants: tuple[str, ...]
    symptom: Symptom
    requires: str | None = None
    note: str | None = None
    also_accept: tuple[str, ...] = ()  # other classes a reader of the telemetry may fairly give this fault


@dataclass(frozen=True, slots=True)
class Catalogue:
    demo_tag: str
    demo_commit: str
    flagd_sha256: str
    off_variant: str
    faults: dict[str, FaultKind]
    excluded: dict[str, str]
    sha256: str

    @staticmethod
    def load(path: Path = DEFAULT_CATALOGUE) -> Catalogue:
        raw = path.read_bytes()
        d = yaml.safe_load(raw)
        if d.get("format") != "otb-faults/1":
            raise ScenarioError(f"{path}: not an otb-faults/1 catalogue")
        faults = {}
        for flag, f in d["faults"].items():
            s = dict(f["symptom"])
            symptom = Symptom(None, {}, s["unverified"]) if "unverified" in s else Symptom(s.pop("measure"), s, None)
            faults[flag] = FaultKind(
                flag,
                f["class"],
                f["root_service"],
                tuple(f["variants"]),
                symptom,
                f.get("requires"),
                f.get("note"),
                tuple(f.get("also_accept") or ()),
            )
        return Catalogue(
            demo_tag=str(d["demo_tag"]),
            demo_commit=d["demo_commit"],
            flagd_sha256=d["flagd_sha256"],
            off_variant=d["off_variant"],
            faults=faults,
            excluded=dict(d.get("excluded") or {}),
            sha256=sha256_bytes(raw),
        )

    def check_against_flagd(self, flagd_json: bytes, *, pinned: bool = True) -> list[str]:
        """Problems between the catalogue and a flagd config: the pinned file's sha, then every flag and variant.
        With pinned=False (a live config, which flagd-ui rewrites) only flags and variants are compared."""
        problems = []
        if pinned and sha256_bytes(flagd_json) != self.flagd_sha256:
            problems.append(f"flagd file sha256 {sha256_bytes(flagd_json)} != pinned {self.flagd_sha256}")
        flags = orjson.loads(flagd_json)["flags"]
        for flag in sorted(set(flags) - set(self.faults) - set(self.excluded)):
            problems.append(f"flag {flag} is in flagd but neither catalogued nor excluded")
        for flag, f in sorted(self.faults.items()):
            if flag not in flags:
                problems.append(f"catalogued flag {flag} is not in flagd")
                continue
            have = set(flags[flag]["variants"]) - {self.off_variant}
            if have != set(f.variants):
                problems.append(f"{flag}: variants {sorted(have)} != catalogue {sorted(f.variants)}")
            if self.off_variant not in flags[flag]["variants"]:
                problems.append(f"{flag}: no {self.off_variant!r} variant")
        return problems


# ---------- scenario ----------


@dataclass(frozen=True, slots=True)
class Range:
    lo: int
    hi: int

    def draw(self, rng: random.Random) -> int:
        return rng.randint(self.lo, self.hi)


@dataclass(frozen=True, slots=True)
class FaultSlot:
    id: str
    flag_in: tuple[str, ...]
    variant_in: tuple[str, ...] | None
    start_s: Range
    duration_s: Range
    instance: str
    group: str | None = None
    relation: Literal["overlap", "back_to_back", "cascade", "deploy"] | None = None
    params: dict[str, str] = field(default_factory=dict)
    kind: Literal["fault"] = "fault"


@dataclass(frozen=True, slots=True)
class DecoySlot:
    id: str
    type: DecoyType
    target_in: tuple[str, ...]
    start_s: Range
    duration_s: Range
    instance: str = "*"
    params: dict[str, str] = field(default_factory=dict)
    kind: Literal["decoy"] = "decoy"


@dataclass(frozen=True, slots=True)
class QuietSlot:
    id: str
    start_s: Range
    duration_s: Range
    kind: Literal["quiet"] = "quiet"


Slot = FaultSlot | DecoySlot | QuietSlot


@dataclass(frozen=True, slots=True)
class LoadStep:
    at_s: int
    users: int


@dataclass(frozen=True, slots=True)
class Background:
    """Generator volume beside the demo instances (deploy/k8s/background/), in OTLP/JSON bytes per second as the
    gateway receives it (10^6 bytes), split between logs and metrics. It runs for the whole scenario, carries no
    fault, and no question is scoped to it."""

    mb_per_s: float
    logs_share: float

    @staticmethod
    def parse(d: dict) -> Background:
        if set(d) != {"mb_per_s", "logs_share"}:
            raise ScenarioError(f"background needs exactly mb_per_s and logs_share, got {sorted(d)}")
        b = Background(float(d["mb_per_s"]), float(d["logs_share"]))
        if b.mb_per_s < 0 or not 0 <= b.logs_share <= 1:
            raise ScenarioError(f"background {d}: mb_per_s must be >= 0 and logs_share in [0, 1]")
        return b

    def bytes(self, duration_s: int) -> float:
        return self.mb_per_s * 1e6 * duration_s


@dataclass(frozen=True, slots=True)
class Scenario:
    id: str
    seed: int | None
    duration_s: int
    instances: tuple[str, ...]
    load: tuple[LoadStep, ...]
    min_gap_s: int
    slots: tuple[Slot, ...]
    sha256: str
    background: Background | None = None
    runtime: str = "compose"  # where the demo runs: compose (the laptop capture) or k8s (otb-demo-<instance>)


def _range(d: dict) -> Range:
    r = Range(int(d["lo"]), int(d["hi"]))
    if r.lo > r.hi or r.lo < 0:
        raise ScenarioError(f"bad range {d}")
    return r


def load_scenario(path: Path) -> Scenario:
    raw = path.read_bytes()
    d = yaml.safe_load(raw)
    slots: list[Slot] = []
    for s in d.get("slots") or ():
        match s["kind"]:
            case "fault":
                slots.append(
                    FaultSlot(
                        id=s["id"],
                        flag_in=tuple(s["flag_in"]),
                        variant_in=tuple(s["variant_in"]) if s.get("variant_in") else None,
                        start_s=_range(s["start_s"]),
                        duration_s=_range(s["duration_s"]),
                        instance=s.get("instance", "*"),
                        group=s.get("group"),
                        relation=s.get("relation"),
                        params={k: str(v) for k, v in (s.get("params") or {}).items()},
                    )
                )
            case "decoy":
                slots.append(
                    DecoySlot(
                        id=s["id"],
                        type=s["type"],
                        target_in=tuple(s["target_in"]),
                        start_s=_range(s["start_s"]),
                        duration_s=_range(s["duration_s"]),
                        instance=s.get("instance", "*"),
                        params={k: str(v) for k, v in (s.get("params") or {}).items()},
                    )
                )
            case "quiet":
                slots.append(QuietSlot(s["id"], _range(s["start_s"]), _range(s["duration_s"])))
            case other:
                raise ScenarioError(f"{path}: slot {s.get('id')} has unknown kind {other!r}")
    for s in slots:
        if isinstance(s, FaultSlot) and s.relation == "deploy" and (s.group or "version_to" not in s.params):
            raise ScenarioError(f"{path}: deploy slot {s.id} needs params.version_to and no group")
    ids = [s.id for s in slots]
    if len(ids) != len(set(ids)):
        raise ScenarioError(f"{path}: duplicate slot ids")
    runtime = d.get("runtime", "compose")
    if runtime not in ("compose", "k8s"):
        raise ScenarioError(f"{path}: runtime must be compose or k8s, not {runtime!r}")
    return Scenario(
        id=d["id"],
        seed=d.get("seed"),
        duration_s=int(d["duration_s"]),
        instances=tuple(d["instances"]),
        load=tuple(LoadStep(int(x["at_s"]), int(x["users"])) for x in d.get("load") or ()),
        min_gap_s=int(d.get("min_gap_s", 0)),
        slots=tuple(slots),
        sha256=sha256_bytes(raw),
        background=Background.parse(d["background"]) if d.get("background") is not None else None,
        runtime=runtime,
    )


# ---------- plan (pure) ----------


@dataclass(frozen=True, slots=True)
class PlannedEntry:
    slot_id: str
    kind: Literal["fault", "decoy", "quiet", "load"]
    start_offset_s: int
    end_offset_s: int
    instances: tuple[str, ...]
    flag: str | None = None
    variant: str | None = None
    fault_class: str | None = None
    root_service: str | None = None
    decoy_type: str | None = None
    target: str | None = None
    params: dict[str, str] = field(default_factory=dict)
    group: str | None = None
    relation: str | None = None


@dataclass(frozen=True, slots=True)
class Plan:
    scenario_id: str
    scenario_sha256: str
    catalogue_sha256: str
    seed: int
    duration_s: int
    entries: tuple[PlannedEntry, ...]

    def to_yaml(self) -> bytes:
        d = asdict(self)
        d["entries"] = [{k: v for k, v in e.items() if v not in (None, {})} for e in d["entries"]]
        return _dump({"format": "otb-plan/1", **d})

    @staticmethod
    def from_yaml(raw: bytes) -> Plan:
        d = yaml.safe_load(raw)
        d.pop("format")
        entries = tuple(PlannedEntry(**(e | {"instances": tuple(e["instances"])})) for e in d.pop("entries"))
        return Plan(**d, entries=entries)


MAX_DRAWS = 1000


def _instances(spec: str, scenario: Scenario, rng: random.Random) -> tuple[str, ...]:
    if spec == "*":
        return scenario.instances
    if spec == "any":
        return (rng.choice(sorted(scenario.instances)),)
    if spec not in scenario.instances:
        raise ScenarioError(f"instance {spec!r} is not one of {scenario.instances}")
    return (spec,)


def _draw_fault(slot: FaultSlot, cat: Catalogue, rng: random.Random) -> tuple[str, str]:
    flags = sorted(f for f in slot.flag_in if f not in cat.excluded)
    unknown = [f for f in flags if f not in cat.faults]
    if unknown or not flags:
        raise ScenarioError(f"slot {slot.id}: flags {unknown or slot.flag_in} not usable from the catalogue")
    flag = rng.choice(flags)
    have = cat.faults[flag].variants
    variants = sorted(v for v in have if slot.variant_in is None or v in slot.variant_in)
    if not variants:
        raise ScenarioError(f"slot {slot.id}: none of {slot.variant_in} is a variant of {flag} {have}")
    return flag, rng.choice(variants)


def _clash(a: PlannedEntry, b: PlannedEntry, gap_s: int) -> bool:
    return a.start_offset_s < b.end_offset_s + gap_s and b.start_offset_s < a.end_offset_s + gap_s


def plan(scenario: Scenario, catalogue: Catalogue, seed: int) -> Plan:
    """Pure: the same (scenario, catalogue, seed) gives the same plan, byte for byte. Each slot draws from its own
    generator seeded with (seed, slot id), so adding a slot does not move the others. Ungrouped slots are placed
    first, then grouped ones, each in id order; a slot that lands within min_gap_s of an unrelated placed slot is
    redrawn. A grouped slot is placed relative to its group's first slot: overlap and cascade start inside it,
    back_to_back starts after it."""
    placed: list[PlannedEntry] = []
    anchors: dict[str, PlannedEntry] = {}
    gap = scenario.min_gap_s
    for slot in sorted(scenario.slots, key=lambda s: (getattr(s, "group", None) is not None, s.id)):
        rng = random.Random(f"{seed}:{slot.id}")
        for _ in range(MAX_DRAWS):
            entry = _draw(slot, scenario, catalogue, rng, anchors)
            if entry.end_offset_s > scenario.duration_s:
                continue
            related = {e.slot_id for e in placed if e.group is not None and e.group == entry.group}
            if not any(_clash(entry, e, gap) for e in placed if e.slot_id not in related):
                break
        else:
            raise ScenarioError(f"slot {slot.id}: no placement in {MAX_DRAWS} draws satisfies min_gap_s={gap}")
        placed.append(entry)
        if isinstance(slot, FaultSlot) and slot.group and slot.group not in anchors:
            anchors[slot.group] = entry
    loads = [
        PlannedEntry(f"load{i}", "load", step.at_s, step.at_s, scenario.instances, params={"users": str(step.users)})
        for i, step in enumerate(scenario.load)
    ]
    entries = tuple(sorted(placed + loads, key=lambda e: (e.start_offset_s, e.slot_id)))
    return Plan(scenario.id, scenario.sha256, catalogue.sha256, seed, scenario.duration_s, entries)


def _draw(
    slot: Slot, scenario: Scenario, cat: Catalogue, rng: random.Random, anchors: dict[str, PlannedEntry]
) -> PlannedEntry:
    duration = slot.duration_s.draw(rng)
    if isinstance(slot, FaultSlot):
        flag, variant = _draw_fault(slot, cat, rng)
        anchor = anchors.get(slot.group) if slot.group else None
        offset = slot.start_s.draw(rng)
        if anchor is None:
            start = offset
        elif slot.relation == "back_to_back":
            start = anchor.end_offset_s + offset
        else:
            start = anchor.start_offset_s + offset
            if start >= anchor.end_offset_s:
                raise ScenarioError(f"slot {slot.id}: start_s puts it after its {slot.relation} anchor ends")
        f = cat.faults[flag]
        return PlannedEntry(
            slot.id,
            "fault",
            start,
            start + duration,
            _instances(slot.instance, scenario, rng),
            flag=flag,
            variant=variant,
            fault_class="bad_deploy" if slot.relation == "deploy" else f.fault_class,
            root_service=f.root_service,
            target=f.root_service if slot.relation == "deploy" else None,
            params=slot.params,
            group=slot.group,
            relation=slot.relation,
        )
    start = slot.start_s.draw(rng)
    if isinstance(slot, DecoySlot):
        return PlannedEntry(
            slot.id,
            "decoy",
            start,
            start + duration,
            _instances(slot.instance, scenario, rng),
            decoy_type=slot.type,
            target=rng.choice(sorted(slot.target_in)),
            params=slot.params,
        )
    return PlannedEntry(slot.id, "quiet", start, start + duration, scenario.instances)


# ---------- realised schedule ----------


@dataclass(frozen=True, slots=True)
class ActionRecord:
    at_ns: int
    action: str
    ok: bool
    detail: dict[str, str]


@dataclass(frozen=True, slots=True)
class RealisedEntry:
    slot_id: str
    kind: Literal["fault", "decoy", "quiet"]
    status: Literal["realised", "failed"]
    instance: str
    window: Window
    flag: str | None = None
    variant: str | None = None
    fault_class: str | None = None
    root_service: str | None = None
    decoy_type: str | None = None
    target: str | None = None
    group: str | None = None
    relation: str | None = None
    actions: tuple[ActionRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class RealisedSchedule:
    scenario_id: str
    seed: int | None
    plan_sha256: str | None
    capture_start_ns: int
    entries: tuple[RealisedEntry, ...]
    unplanned: tuple[RealisedEntry, ...] = ()

    def to_yaml(self) -> bytes:
        def entry(e: RealisedEntry) -> dict:
            d = asdict(e)
            d["window"] = {"start": e.window.start, "end": e.window.end}
            d["actions"] = [asdict(a) for a in e.actions]
            return {k: v for k, v in d.items() if v is not None}

        return _dump(
            {
                "format": "otb-schedule/1",
                "scenario_id": self.scenario_id,
                "seed": self.seed,
                "plan_sha256": self.plan_sha256,
                "capture_start_ns": self.capture_start_ns,
                "entries": [entry(e) for e in self.entries],
                "unplanned": [entry(e) for e in self.unplanned],
            }
        )

    @staticmethod
    def from_yaml(raw: bytes) -> RealisedSchedule:
        d = yaml.safe_load(raw)
        if d.get("format") != "otb-schedule/1":
            raise ScenarioError("not an otb-schedule/1 file")

        def entry(e: dict) -> RealisedEntry:
            w = e.pop("window")
            actions = tuple(ActionRecord(**a) for a in e.pop("actions", ()))
            return RealisedEntry(**e, window=Window(EventNs(w["start"]), EventNs(w["end"])), actions=actions)

        return RealisedSchedule(
            scenario_id=d["scenario_id"],
            seed=d["seed"],
            plan_sha256=d["plan_sha256"],
            capture_start_ns=d["capture_start_ns"],
            entries=tuple(entry(e) for e in d["entries"]),
            unplanned=tuple(entry(e) for e in d.get("unplanned") or ()),
        )


def schedule_from_events(p: Plan, plan_sha256: str, capture_start_ns: int, events: list[dict]) -> RealisedSchedule:
    """Pure. One entry per (slot, instance) that the injector acted on. A fault is realised when both its on and
    off were confirmed by readback; its window is [on confirmed, off confirmed). Anything else is failed and binds
    nothing. A decoy is realised when its start and end actions succeeded, a quiet window when the injector marked
    its start and end; the window is [start, end). A release (a deploy fault, or a benign_deploy the injector
    drove as one) is confirmed by its deploy.confirm events: the release ready (with a fault's flag on), then the
    rollback ready."""
    by_key: dict[tuple[str, str], list[dict]] = {}
    for ev in events:
        if ev.get("slot_id"):
            by_key.setdefault((ev["slot_id"], ev["instance"]), []).append(ev)
    out = []
    for pe in p.entries:
        if pe.kind == "load":
            continue
        for inst in pe.instances:
            evs = by_key.get((pe.slot_id, inst))
            if not evs:
                continue
            actions = tuple(
                ActionRecord(e["at_ns"], e["action"], e["ok"], e.get("detail", {}) | {"phase": e["phase"]}) for e in evs
            )
            released = pe.relation == "deploy" or any(e["action"] == "deploy.confirm" for e in evs)
            if pe.kind == "fault" or released:
                confirm = "deploy.confirm" if released else "flag.confirm"
                on = next((e for e in evs if e["action"] == confirm and e["phase"] == "on" and e["ok"]), None)
                off = next((e for e in evs if e["action"] == confirm and e["phase"] == "off" and e["ok"]), None)
            else:
                on = next((e for e in evs if e["phase"] == "on" and e["ok"]), None)
                off = next((e for e in evs if e["phase"] == "off" and e["ok"]), None)
            realised = on is not None and off is not None and off["at_ns"] > on["at_ns"]
            start = on["at_ns"] if on else evs[0]["at_ns"]
            end = off["at_ns"] if off and off["at_ns"] > start else start + 1
            out.append(
                RealisedEntry(
                    slot_id=pe.slot_id,
                    kind=pe.kind,
                    status="realised" if realised else "failed",
                    instance=inst,
                    window=Window(EventNs(start), EventNs(end)),
                    flag=pe.flag,
                    variant=pe.variant,
                    fault_class=pe.fault_class,
                    root_service=pe.root_service,
                    decoy_type=pe.decoy_type,
                    target=pe.target,
                    group=pe.group,
                    relation=pe.relation,
                    actions=actions,
                )
            )
    return RealisedSchedule(p.scenario_id, p.seed, plan_sha256, capture_start_ns, tuple(out))


def synthesise_v2_schedule(
    corpus_cfg: dict, catalogue: Catalogue, capture_start_ns: int, corpus_end_ns: int
) -> tuple[bytes, RealisedSchedule]:
    """For `corpus import-v2`: a hand-run v2 capture had no plan. Its corpus.yaml events become the realised
    schedule: each `fault` event an entry, the load change and the traffic gap unplanned decoys. An event time is
    HH:MM:SS on whichever day of window_utc puts it inside the window padded by an hour, so a capture may run past
    midnight. Every window must lie inside the capture [capture_start_ns, corpus_end_ns]; a slice of a longer
    capture (corpus.yaml `slice_of`) may cut a window's end. Returns the synthesised scenario.yaml bytes and the
    schedule."""
    HOUR = 3600 * NS
    lo, hi = (int(datetime.fromisoformat(x).replace(tzinfo=UTC).timestamp()) * NS for x in corpus_cfg["window_utc"])

    def ns(hhmmss: str) -> int:
        days = range((lo - HOUR) // (86_400 * NS), (hi + HOUR) // (86_400 * NS) + 1)
        h, m, sec = (int(x) for x in hhmmss.split(":"))
        fits = [t for d in days if lo - HOUR <= (t := (d * 86_400 + h * 3600 + m * 60 + sec) * NS) <= hi + HOUR]
        if not fits:
            raise ScenarioError(f"event time {hhmmss} lies in no day of the window {corpus_cfg['window_utc']}")
        if len(fits) > 1:
            raise ScenarioError(f"event time {hhmmss} fits more than one day of the window {corpus_cfg['window_utc']}")
        return fits[0]

    src = {"source": "corpus.yaml events (v2 measure.log)"}
    entries, unplanned = [], []
    for ev in corpus_cfg.get("events") or ():
        kind = ev["kind"]
        if kind == "fault":
            f = catalogue.faults[ev["flag"]]
            start, end = ns(ev["from"]), ns(ev["to"])
            actions = (
                ActionRecord(start, "flag.write", True, src | {"phase": "on", "variant": str(ev["value"])}),
                ActionRecord(end, "flag.write", True, src | {"phase": "off", "variant": catalogue.off_variant}),
            )
            entries.append(
                RealisedEntry(
                    f"f{len(entries) + 1}",
                    "fault",
                    "realised",
                    "default",
                    Window(EventNs(start), EventNs(end)),
                    flag=ev["flag"],
                    variant=str(ev["value"]),
                    fault_class=f.fault_class,
                    root_service=f.root_service,
                    actions=actions,
                )
            )
        elif kind in ("load", "gap"):
            start = ns(ev.get("at") or ev["from"])
            end = ns(ev["to"]) if "to" in ev else corpus_end_ns
            unplanned.append(
                RealisedEntry(
                    f"u{len(unplanned) + 1}",
                    "decoy",
                    "realised",
                    "default",
                    Window(EventNs(start), EventNs(end)),
                    decoy_type="load_step" if kind == "load" else "traffic_gap",
                    target="load-generator",
                    actions=(ActionRecord(start, f"v2.{kind}", True, src | {"note": ev.get("note", "")}),),
                )
            )
    end_bound = None if "slice_of" in corpus_cfg else corpus_end_ns
    for e in entries + unplanned:
        if not capture_start_ns <= e.window.start < e.window.end <= (end_bound or e.window.end):
            w = f"[{render_utc(e.window.start, 's')}, {render_utc(e.window.end, 's')})"
            raise ScenarioError(f"{e.slot_id} {w} lies outside the capture")
    scenario = _dump(
        {
            "id": "dryrun-v2-import",
            "seed": None,
            "duration_s": (corpus_end_ns - capture_start_ns) // NS,
            "instances": ["default"],
            "min_gap_s": 0,
            "note": "hand-run v2 capture: no plan existed; schedule.yaml is synthesised from corpus.yaml events",
            "slots": [],
        }
    )
    return scenario, RealisedSchedule(
        "dryrun-v2-import", None, None, capture_start_ns, tuple(entries), tuple(unplanned)
    )


# ---------- expected bytes ----------

DEFAULT_RATES = REPO / "scenarios" / "rates.yaml"


@dataclass(frozen=True, slots=True)
class LoadBytes:
    from_s: int
    to_s: int
    users: int
    instances: int
    mb_per_min: dict[str, float]
    extrapolated: bool

    @property
    def bytes(self) -> float:
        return sum(self.mb_per_min.values()) * 1e6 * (self.to_s - self.from_s) / 60 * self.instances


def rate_at(points: list[dict], users: int) -> tuple[dict[str, float], bool]:
    """Linear in users through the two nearest measured points; outside the measured range, the end segment's
    line is extended and the rate is marked extrapolated."""
    pts = sorted(points, key=lambda p: p["users"])
    lo, hi = pts[0], pts[-1]
    for a, b in pairwise(pts):
        if a["users"] <= users <= b["users"]:
            lo, hi = a, b
            break
    else:
        lo, hi = (pts[0], pts[1]) if users < pts[0]["users"] else (pts[-2], pts[-1])
    f = (users - lo["users"]) / (hi["users"] - lo["users"])
    signals = ("traces", "logs", "metrics")
    out = {s: round(lo[s] + f * (hi[s] - lo[s]), 4) for s in signals}
    return out, not (pts[0]["users"] <= users <= pts[-1]["users"])


def expected_bytes(scenario: Scenario, rates_path: Path = DEFAULT_RATES) -> list[LoadBytes]:
    """OTLP/JSON bytes per load step from measured per-instance rates. Decoy load steps are excluded: they are
    short and on one instance. The scenario's background volume is not a load step; Background.bytes has it."""
    points = yaml.safe_load(rates_path.read_bytes())["points"]
    steps = sorted(scenario.load, key=lambda s: s.at_s)
    out = []
    for i, step in enumerate(steps):
        end = steps[i + 1].at_s if i + 1 < len(steps) else scenario.duration_s
        mb, extrapolated = rate_at(points, step.users)
        out.append(LoadBytes(step.at_s, end, step.users, len(scenario.instances), mb, extrapolated))
    return out


def format_bytes(scenario: Scenario, rows: list[LoadBytes]) -> str:
    lines = [f"scenario {scenario.id}: {len(scenario.instances)} instances, {scenario.duration_s / 3600:g} h"]
    lines.append("  from_h  to_h  users  MB/min/instance (traces+logs+metrics)        GB")
    for r in rows:
        mb = r.mb_per_min
        total = sum(mb.values())
        note = "  (extrapolated: above the highest measured load)" if r.extrapolated else ""
        lines.append(
            f"  {r.from_s / 3600:6g} {r.to_s / 3600:5g} {r.users:6d}  "
            f"{mb['traces']:.2f}+{mb['logs']:.2f}+{mb['metrics']:.2f} = {total:.2f} x {r.instances} x "
            f"{(r.to_s - r.from_s) / 60:g} min = {r.bytes / 1e9:8.1f}{note}"
        )
    grand = sum(r.bytes for r in rows)
    if bg := scenario.background:
        bg_bytes = bg.bytes(scenario.duration_s)
        lines.append(
            f"  background {bg.mb_per_s:g} MB/s (logs {bg.logs_share:.0%}, metrics {1 - bg.logs_share:.0%}) x "
            f"{scenario.duration_s / 60:g} min = {bg_bytes / 1e9:.1f} GB"
        )
        grand += bg_bytes
    lines.append(f"  expected OTLP/JSON total: {grand / 1e9:.1f} GB ({grand / 1e12:.3f} TB)")
    return "\n".join(lines)
