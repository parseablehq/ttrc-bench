"""schedule_check: what each realised schedule entry actually did to the telemetry, from the reference store only.

Faults. The catalogue's symptom measure runs over the search window [applied - SLACK_S, released + TAIL_S), where
applied and released are the injector's own on and off actions, and over a baseline of up to BASELINE_S just
before it. A signature is a symptom when its per-minute rate in the search window exceeds its baseline rate; the
one with the largest excess over its baseline count wins. Onset is its first occurrence in the first BUCKET_S
bucket of the search window whose count exceeds the baseline rate, offset its last occurrence in the last such
bucket, so a stray background occurrence near the edges does not move them. No symptom: status no_symptom.

Decoys. A decoy is realised when its effect is seen, not when its command succeeded: benign_restart needs a new
process start of the target (a cumulative series whose start time falls in the window, or an instance id first
seen there); benign_deploy needs a service.version of the target first seen there; traffic_gap needs the SERVER
span rate to fall to GAP_MAX_RATIO of the baseline rate or below; load_step needs it to move by STEP_MIN_RATIO
either way. Otherwise status no_effect.

Quiet. Every decoy and quiet window gets `quiet`: no service has QUIET_MIN_ERRORS or more error spans at more than
QUIET_MAX_RATIO times its baseline error rate. `twin_host` turns a check into the reason it cannot host a
negative-control twin, or None. The result goes to derived/schedule_check.json, never back into schedule.yaml.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

import orjson

from otb.archive import Archive
from otb.ref.store import ReferenceStore
from otb.scenario import Catalogue, RealisedEntry, RealisedSchedule
from otb.timebase import EventNs, Window, render_utc

NS = 1_000_000_000
SLACK_S = 5  # the injector's clock against the telemetry's timestamps
TAIL_S = 120  # effects that outlast the release: retried requests, batched logs
BASELINE_S = 1800
BUCKET_S = 60
QUIET_MIN_ERRORS = 5
QUIET_MAX_RATIO = 2.0
GAP_MAX_RATIO = 0.5
STEP_MIN_RATIO = 1.25


@dataclass(frozen=True, slots=True)
class Observation:
    slot_id: str
    instance: str
    kind: str
    status: str
    declared_start_ns: int
    declared_end_ns: int
    search_start_ns: int | None = None
    search_end_ns: int | None = None
    baseline_start_ns: int | None = None
    onset_ns: int | None = None
    offset_ns: int | None = None
    onset_utc: str | None = None
    offset_utc: str | None = None
    onset_delay_s: float | None = None
    offset_delay_s: float | None = None
    signature: str | None = None
    affected_records: int = 0
    baseline_records: int = 0
    measure: str | None = None
    params: dict | None = None
    effect: dict | None = None
    quiet: bool | None = None
    loud_services: list[str] | None = None
    reason: str | None = None


def _with(o: Observation, **changes: object) -> Observation:
    return Observation(**(asdict(o) | changes))


def _acted(e: RealisedEntry, phase: str) -> int | None:
    times = [a.at_ns for a in e.actions if a.ok and a.detail.get("phase") == phase]
    return min(times) if times else None


def _minutes(w: Window) -> float:
    return (w.end - w.start) / (60 * NS)


def _baseline(start_ns: int, length_ns: int, capture_start_ns: int) -> Window:
    return Window(EventNs(max(capture_start_ns, start_ns - length_ns)), EventNs(start_ns))


def _run(store: ReferenceStore, name: str, params: dict, instance: str) -> list[dict]:
    """Over the entry's own instance only (`ReferenceStore.scoped`)."""
    return store.scoped(instance).run_measure(name, params).as_dicts()


def observe_fault(store: ReferenceStore, e: RealisedEntry, cat: Catalogue, capture_start_ns: int) -> Observation:
    base = Observation(e.slot_id, e.instance, e.kind, "unchecked", e.window.start, e.window.end)
    symptom = cat.faults[e.flag].symptom  # type: ignore[index]
    if symptom.unverified:
        return _with(base, reason=symptom.unverified)
    applied = _acted(e, "on") or e.window.start
    released = _acted(e, "off") or e.window.end
    search = Window(EventNs(applied - SLACK_S * NS), EventNs(released + TAIL_S * NS))
    before = _baseline(search.start, BASELINE_S * NS, capture_start_ns)
    params = dict(symptom.params)
    found = _with(
        base,
        measure=symptom.measure,
        params=params,
        search_start_ns=search.start,
        search_end_ns=search.end,
        baseline_start_ns=before.start,
    )
    rows = _run(store, symptom.measure, params | {"window": search}, e.instance)  # type: ignore[arg-type]
    base_n: dict[str, int] = {}
    if before.end > before.start:
        rows_b = _run(store, symptom.measure, params | {"window": before}, e.instance)  # type: ignore[arg-type]
        base_n = {r["signature"]: r["n"] for r in rows_b}
    per_min = {s: n / _minutes(before) for s, n in base_n.items()}
    expected = {r["signature"]: per_min.get(r["signature"], 0.0) * _minutes(search) for r in rows}
    candidates = sorted(
        (r for r in rows if r["n"] > expected[r["signature"]]),
        key=lambda r: (-(r["n"] - expected[r["signature"]]), -r["n"], r["signature"]),
    )
    if not candidates:
        return _with(found, status="no_symptom", reason="no signature above its baseline rate in the search window")
    top = candidates[0]
    sig = top["signature"]
    rising = []
    for start in range(search.start, search.end, BUCKET_S * NS):
        b = Window(EventNs(start), EventNs(min(start + BUCKET_S * NS, search.end)))
        hit = next(
            (r for r in _run(store, symptom.measure, params | {"window": b}, e.instance) if r["signature"] == sig), None
        )  # type: ignore[arg-type]
        if hit is not None and hit["n"] > per_min.get(sig, 0.0) * _minutes(b):
            rising.append(hit)
    onset, offset = rising[0]["first_ns"], rising[-1]["last_ns"]
    return _with(
        found,
        status="realised",
        onset_ns=onset,
        offset_ns=offset,
        onset_utc=render_utc(onset, "s"),
        offset_utc=render_utc(offset, "s"),
        onset_delay_s=round((onset - applied) / NS, 3),
        offset_delay_s=round((offset - released) / NS, 3),
        signature=sig,
        affected_records=top["n"],
        baseline_records=base_n.get(sig, 0),
    )


def _request_rate(store: ReferenceStore, w: Window, instance: str) -> float:
    n = _run(store, "span_count", {"window": w, "kind": "SERVER"}, instance)[0]["n"]
    return n / _minutes(w) if w.end > w.start else 0.0


def decoy_effect(store: ReferenceStore, e: RealisedEntry, before: Window) -> tuple[str, dict]:
    """(status, evidence) for one decoy."""
    if e.decoy_type == "benign_restart":
        w = Window(EventNs(e.window.start - SLACK_S * NS), EventNs(e.window.end + TAIL_S * NS))
        ev = _run(store, "restart_evidence", {"window": w, "service": e.target}, e.instance)[0]
        seen = ev["new_series_starts"] > 0 or ev["new_instances"] > 0
        return ("realised" if seen else "no_effect"), ev
    if e.decoy_type == "benign_deploy":
        w = Window(EventNs(e.window.start - SLACK_S * NS), EventNs(e.window.end + TAIL_S * NS))
        versions = [
            r["version"] for r in _run(store, "service_version_change", {"window": w, "service": e.target}, e.instance)
        ]
        return ("realised" if versions else "no_effect"), {"new_versions": versions}
    if e.decoy_type in ("traffic_gap", "load_step"):
        rate, base = _request_rate(store, e.window, e.instance), _request_rate(store, before, e.instance)
        ratio = rate / base if base else None
        ev = {"server_spans_per_min": round(rate, 3), "baseline_per_min": round(base, 3), "ratio": ratio}
        if ratio is not None:
            ev["ratio"] = round(ratio, 3)
        if e.decoy_type == "traffic_gap":
            seen = ratio is not None and ratio <= GAP_MAX_RATIO
        else:
            seen = ratio is not None and (ratio >= STEP_MIN_RATIO or ratio <= 1 / STEP_MIN_RATIO)
        return ("realised" if seen else "no_effect"), ev
    return "unchecked", {"reason": f"no effect rule for decoy type {e.decoy_type}"}


def observe_window(store: ReferenceStore, e: RealisedEntry, capture_start_ns: int) -> Observation:
    """A decoy or quiet window: its effect (decoys) and whether it is quiet."""
    base = Observation(e.slot_id, e.instance, e.kind, "realised", e.window.start, e.window.end)
    before = _baseline(e.window.start, max(e.window.end - e.window.start, BASELINE_S * NS), capture_start_ns)
    out = _with(base, baseline_start_ns=before.start)
    if e.kind == "decoy":
        status, effect = decoy_effect(store, e, before)
        out = _with(out, status=status, effect=effect)
    params = {"window": e.window, "baseline": before, "min_errors": QUIET_MIN_ERRORS, "min_ratio": QUIET_MAX_RATIO}
    loud = sorted(r["service"] for r in _run(store, "error_rate_ratio_by_service", params, e.instance))
    return _with(out, quiet=not loud, loud_services=loud)


def observe(store: ReferenceStore, e: RealisedEntry, cat: Catalogue, capture_start_ns: int) -> Observation:
    if e.status != "realised":
        why = "the injector did not confirm both on and off"
        return Observation(e.slot_id, e.instance, e.kind, "failed", e.window.start, e.window.end, reason=why)
    if e.kind == "fault":
        return observe_fault(store, e, cat, capture_start_ns)
    return observe_window(store, e, capture_start_ns)


def twin_host(check: Mapping) -> str | None:
    """None when this schedule check's window may host a negative-control twin; otherwise the reason it may not.
    For `otb tasks instantiate`: a twin needs a decoy whose effect was seen, or a quiet window, and no service's
    errors above its baseline band."""
    if check["kind"] == "fault":
        return "a fault window"
    if check["status"] != "realised":
        return f"decoy effect not seen: {check['status']}"
    if not check["quiet"]:
        return f"not quiet: {', '.join(check['loud_services'])} errors above the baseline band"
    return None


def write(corpus: Path, catalogue: Catalogue | None = None) -> Path | None:
    """Writes derived/schedule_check.json when the corpus has a schedule; returns its path."""
    path = corpus / "schedule.yaml"
    if not path.exists():
        return None
    schedule = RealisedSchedule.from_yaml(path.read_bytes())
    cat = catalogue or Catalogue.load()
    store = ReferenceStore.open(corpus)
    entries = schedule.entries + schedule.unplanned
    checks = [asdict(observe(store, e, cat, schedule.capture_start_ns)) for e in entries]
    out = corpus / "derived" / "schedule_check.json"
    doc = {
        "corpus_id": Archive(corpus).manifest.corpus_id,
        "catalogue_sha256": cat.sha256,
        "rules": {
            "slack_s": SLACK_S,
            "tail_s": TAIL_S,
            "baseline_s": BASELINE_S,
            "bucket_s": BUCKET_S,
            "quiet_min_errors": QUIET_MIN_ERRORS,
            "quiet_max_ratio": QUIET_MAX_RATIO,
            "gap_max_ratio": GAP_MAX_RATIO,
            "step_min_ratio": STEP_MIN_RATIO,
        },
        "checks": checks,
    }
    out.write_bytes(orjson.dumps(doc, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS) + b"\n")
    return out
