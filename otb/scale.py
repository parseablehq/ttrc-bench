"""`otb scale-report`: what the recorded evidence says about running a scenario's bytes (default: week-ladder, about
1 TB) through OTB.

It reads only what other commands wrote: the corpus manifest and segment list, the reference build's
`derived/ref/build_report.json` and `built.jsonl`, each gateway report under `derived/gateway/`, one ingest
session's reports, feeder stats (`<stack>.feeder.jsonl`) and stack disk files (`<stack>.disk.json`), and benchmark
outputs (tests/scale/*.py) under `derived/scale/` or named with --bench. It writes `scale-report.md` and
`scale-report.json` next to the benchmarks.

Two tables. Measured: each component's rate and peak RSS, with the host it ran on and the file it came from.
Arithmetic: the scenario's bytes against those rates, each row marked measured (the number is a measurement),
extrapolated (a measurement carried to another size, worker count or host), scenario (from scenarios/rates.yaml)
or not measured (no evidence; the row says what to run).
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from otb.cli import command
from otb.stacks import registry

REPO = Path(__file__).resolve().parent.parent
DEFAULT_SCENARIO = REPO / "scenarios" / "week-ladder.yaml"
MB = 1e6
GB = 1e9
Basis = Literal["measured", "extrapolated", "scenario", "not measured"]


@dataclass(frozen=True, slots=True)
class Measure:
    component: Literal["gateway", "reference build", "feeder", "feeder preparation"]
    what: str
    raw_mb_per_s: float | None  # raw OTLP/JSON bytes per wall second
    raw_mb_per_cpu_s: float | None  # per CPU second: what one busy core would do
    peak_rss_mb: float | None
    capacity: bool  # True: driven as fast as it would go; False: paced by its input (a capture, a 1x replay)
    host: dict | None
    source: str
    parallel: int = 1  # workers or processes behind raw_mb_per_s


@dataclass(frozen=True, slots=True)
class Row:
    quantity: str
    value: str
    basis: Basis
    how: str


@dataclass(frozen=True, slots=True)
class CorpusFacts:
    name: str
    corpus_id: str
    duration_s: float
    archive_bytes: int
    raw_bytes: int | None  # None: no reference build covers every segment
    parquet_bytes: int | None


# ---------- reading the evidence ----------


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO))
    except ValueError:
        return str(path.resolve())


def _mb(b: float, s: float) -> float | None:
    return round(b / MB / s, 2) if s else None


def corpus_facts(corpus: Path) -> CorpusFacts:
    from otb.archive import Archive

    archive = Archive(corpus)
    m = archive.manifest
    segs = archive.segments()
    ref = corpus / "derived" / "ref"
    raw_by_sha: dict[str, int] = {}
    if (ref / "built.jsonl").exists():
        for line in (ref / "built.jsonl").read_text().splitlines():
            if line.strip():
                b = json.loads(line)
                raw_by_sha[b["segment_sha256"]] = b["raw_bytes"]
    covered = all(s.sha256 in raw_by_sha for s in segs) and bool(segs)
    parquet = sum(p.stat().st_size for p in ref.glob("*/*.parquet")) if covered else None
    w = m.window
    return CorpusFacts(
        name=m.name,
        corpus_id=m.corpus_id or "unsealed",
        duration_s=(w.end - w.start) / 1e9 if w is not None else 0.0,
        archive_bytes=sum(s.bytes for s in segs),
        raw_bytes=sum(raw_by_sha[s.sha256] for s in segs) if covered else None,
        parquet_bytes=parquet,
    )


def gateway_measures(corpus: Path) -> list[Measure]:
    out = []
    for p in sorted((corpus / "derived" / "gateway").glob("*.json")):
        r = json.loads(p.read_text())
        cpu = r["cpu_user_s"] + r["cpu_sys_s"]
        raw = r["totals"]["raw_bytes"]
        out.append(
            Measure(
                "gateway",
                f"stream {r['stream']}, {r['totals']['frames']} frames in {r['active_s']} s of capture",
                r["raw_mb_per_s_active"],
                _mb(raw, cpu),
                r["peak_rss_mb"],
                False,
                r.get("host"),
                _rel(p),
            )
        )
    return out


def ref_measures(corpus: Path) -> list[Measure]:
    p = corpus / "derived" / "ref" / "build_report.json"
    if not p.exists():
        return []
    r = json.loads(p.read_text())
    if not r["segments_built"]:
        return []
    return [
        Measure(
            "reference build",
            f"{r['workers']} workers, {r['segments_built']} segments, {r['bytes_in_raw'] / MB:.1f} MB raw",
            r["mb_per_s_raw"],
            None,
            round(r["peak_rss_worker_bytes"] / 2**20, 1),
            True,
            r.get("host"),
            _rel(p),
            r["workers"],
        )
    ]


def session_measures(session_dir: Path) -> tuple[list[Measure], dict[str, dict], list[str]]:
    """Per stack: its feeder processes' rate and RSS, its disk use if measured ({stack: disk.json}), and notes
    on what the session lacks."""
    d = json.loads((session_dir / "session.json").read_text())
    measures, disks, notes = [], {}, []
    for stack in d["stacks"]:
        stats_path = session_dir / "ingest" / f"{stack}.feeder.jsonl"
        if stats_path.exists():
            runs = [json.loads(x) for x in stats_path.read_text().splitlines() if x.strip()]
            raw = sum(r["raw_bytes"] for r in runs)
            wall = sum(r["wall_s"] for r in runs)
            cpu = sum(r["cpu_user_s"] + r["cpu_sys_s"] for r in runs)
            measures.append(
                Measure(
                    "feeder",
                    f"{stack}, {d['mode']} with sends, {len(runs)} process run(s), session {d['id']}",
                    _mb(raw, wall),
                    _mb(raw, cpu),
                    round(max(r["peak_rss_bytes"] for r in runs) / 2**20, 1),
                    False,
                    runs[-1].get("host"),
                    _rel(stats_path),
                )
            )
        else:
            notes.append(f"session {d['id']}: {stack} has no feeder stats (the session predates U10)")
        disk = session_dir / "ingest" / f"{stack}.disk.json"
        if disk.exists():
            disks[stack] = json.loads(disk.read_text()) | {"source": _rel(disk)}
    return measures, disks, notes


def bench_measures(path: Path) -> list[Measure]:
    """tests/scale outputs, by shape: gateway_throughput (`modes`), feeder_cpu (`replay` or the U12 `one_feeder`),
    ingest_throughput (`benchmark` = ingest_throughput)."""
    d = json.loads(path.read_text())
    src, host = _rel(path), d.get("host")
    out = []
    if d.get("benchmark") == "ingest_throughput":
        for stack, r in d["feeders"].items():
            out.append(
                Measure(
                    "feeder",
                    f"{stack}, replay with sends to a stand-in collector, 1 of {len(d['feeders'])} processes",
                    r["raw_mb_per_s"],
                    r["raw_mb_per_cpu_s"],
                    round(r["peak_rss_bytes"] / 2**20, 1),
                    True,
                    host,
                    src,
                )
            )
    elif "modes" in d:
        for name, r in d["modes"].items():
            out.append(
                Measure(
                    "gateway",
                    f"benchmark {name}: {r['clients']} clients{', gzip' if r['gzip'] else ''}",
                    r["raw_mb_per_s"],
                    round(r["raw_mb"] / r["gateway"]["cpu_s"], 2) if r["gateway"]["cpu_s"] else None,
                    r["gateway"]["peak_rss_mb"],
                    True,
                    host,
                    src,
                )
            )
    elif "replay" in d:
        for mode in ("replay", "follow"):
            for k, r in d.get(mode, {}).items():
                out.append(
                    Measure(
                        "feeder preparation",
                        f"{mode}, {k.replace('_', ' ')}{f' x{r["n"]}' if 'n' in r else ''}, no sends",
                        r["raw_mb_per_s"],
                        r.get("raw_mb_per_cpu_s"),
                        None,
                        True,
                        host,
                        src,
                        r.get("n", 1),
                    )
                )
    elif "one_feeder" in d:
        out.append(
            Measure(
                "feeder preparation",
                "replay, one process, no sends",
                d["one_feeder"]["raw_mb_per_s"],
                None,
                None,
                True,
                host,
                src,
            )
        )
        n = d["feeders_in_one_process"]
        out.append(
            Measure(
                "feeder preparation",
                f"replay, {n['n']} threads in one process, no sends",
                n["raw_mb_per_s"],
                None,
                None,
                True,
                host,
                src,
            )
        )
    return out


# ---------- arithmetic ----------


@dataclass(frozen=True, slots=True)
class ScenarioLoad:
    id: str
    duration_s: int
    total_bytes: float
    peak_bytes_per_s: float
    peak_extrapolated: bool


def scenario_load(path: Path) -> ScenarioLoad:
    from otb.scenario import expected_bytes, load_scenario

    sc = load_scenario(path)
    rows = expected_bytes(sc)
    peak = max(rows, key=lambda r: r.bytes / (r.to_s - r.from_s))
    # the background runs at a constant rate for the whole scenario, on top of every load step
    bg_per_s = sc.background.mb_per_s * 1e6 if sc.background else 0.0
    return ScenarioLoad(
        sc.id,
        sc.duration_s,
        sum(r.bytes for r in rows) + bg_per_s * sc.duration_s,
        peak.bytes / (peak.to_s - peak.from_s) + bg_per_s,
        peak.extrapolated,
    )


def _host(m: Measure) -> str:
    h = m.host
    if h is None:
        return "host not recorded"
    memory = f", {h['memory_gb']:g} GB" if "memory_gb" in h else ""
    return f"{h['platform']}, {h['cpus']} cpus{memory}"


def _cite(m: Measure, v: float, unit: str) -> str:
    return f"{v:g} {unit} ({m.component}, {m.what}, on {_host(m)})"


def _slowest(ms: Iterable[Measure], key: str) -> tuple[Measure, float] | None:
    got = [(m, getattr(m, key)) for m in ms if getattr(m, key)]
    return min(got, key=lambda x: x[1]) if got else None


Pick = tuple[Measure, float, Basis]


def _first(*ladder: tuple[list[Measure], str, Basis]) -> Pick | None:
    """The slowest measure on the first rung that has one: (measures, the field to read, the basis it gives)."""
    for ms, key, basis in ladder:
        if got := _slowest(ms, key):
            return (*got, basis)
    return None


def _headroom(name: str, pick: Pick | None, need: float, missing: str) -> Row:
    if pick is None:
        return Row(name, "-", "not measured", missing)
    m, v, basis = pick
    return Row(name, f"{v / need:.1f}x", basis, f"{_cite(m, v, 'MB/s')} / {need:.2f} MB/s")


def arithmetic(
    load: ScenarioLoad,
    facts: CorpusFacts,
    measures: list[Measure],
    disks: dict[str, dict],
    stacks: list[str],
    workers: tuple[int, ...],
) -> list[Row]:
    B = load.total_bytes
    avg, peak = B / load.duration_s / MB, load.peak_bytes_per_s / MB
    peak_note = " (its load is above the highest measured rate: extrapolated in rates.yaml)" * load.peak_extrapolated
    rows = [
        Row(f"OTLP/JSON bytes of {load.id}", f"{B / GB:.1f} GB", "scenario", "otb scenario bytes: rates.yaml"),
        Row("Capture rate, average", f"{avg:.2f} MB/s", "scenario", f"{B / GB:.1f} GB / {load.duration_s} s"),
        Row("Capture rate, peak load step", f"{peak:.2f} MB/s", "scenario", f"busiest step{peak_note}"),
    ]

    def by(component: str, *, capacity: bool | None = None, what: str = "") -> list[Measure]:
        return [
            m
            for m in measures
            if m.component == component and (capacity is None or m.capacity == capacity) and what in m.what
        ]

    # Each ladder prefers a capacity measurement (driven flat out) and falls back to one carried over: a paced
    # run's MB per CPU second assumes the component is CPU-bound, and preparation without sends assumes sends
    # cost nothing.
    rows.append(
        _headroom(
            "Gateway headroom at the peak, one stream",
            _first(
                (by("gateway", capacity=True), "raw_mb_per_s", "measured"),
                (by("gateway"), "raw_mb_per_cpu_s", "extrapolated"),
            ),
            peak,
            "run tests/scale/gateway_throughput.py, or capture with otb capture (its report lands in derived/gateway)",
        )
    )
    rows.append(
        _headroom(
            "1x replay headroom per stack (one feeder process each)",
            _first(
                (by("feeder", capacity=True), "raw_mb_per_s", "measured"),
                (by("feeder", what="replay"), "raw_mb_per_cpu_s", "extrapolated"),
                (by("feeder preparation", what="replay, one process"), "raw_mb_per_s", "extrapolated"),
            ),
            peak,
            "run tests/scale/ingest_throughput.py or tests/scale/feeder_cpu.py",
        )
    )
    rows.append(
        _headroom(
            "Follow-ingest headroom per stack during capture",
            _first(
                (by("feeder", what="follow"), "raw_mb_per_cpu_s", "extrapolated"),
                (by("feeder preparation", what="follow, one process"), "raw_mb_per_s", "extrapolated"),
            ),
            peak,
            "run tests/scale/feeder_cpu.py (its follow pass), or otb ingest --mode follow",
        )
    )
    cpu = _slowest(by("feeder"), "raw_mb_per_cpu_s")
    if cpu is not None:
        m, v = cpu
        rows.append(
            Row(
                f"Feeder CPU at the peak, {len(stacks)} stacks",
                f"{len(stacks) * peak / v:.2f} cores",
                "extrapolated",
                f"{len(stacks)} x {peak:.2f} MB/s / {_cite(m, v, 'MB per CPU second')}",
            )
        )

    ref = by("reference build")
    if ref and ref[0].raw_mb_per_s:
        m = ref[0]
        for w in workers:
            rate = m.raw_mb_per_s * w / m.parallel
            how = f"{B / GB:.1f} GB / {rate:.1f} MB/s from {_cite(m, m.raw_mb_per_s, 'MB/s')}"
            if w != m.parallel:
                how += f", linear in workers; needs {w} free cores and a disk that keeps up"
            rows.append(Row(f"Reference build time, {w} workers", f"{B / MB / rate / 3600:.2f} h", "extrapolated", how))
    else:
        rows.append(Row("Reference build time", "-", "not measured", "otb ref build CORPUS"))

    if facts.raw_bytes:
        ratio = facts.raw_bytes / facts.archive_bytes
        rows.append(
            Row(
                "Archive on disk",
                f"{B / ratio / GB:.1f} GB",
                "extrapolated",
                f"{B / GB:.1f} GB / {ratio:.2f} (raw / archive on {facts.name}: "
                f"{facts.raw_bytes / MB:.1f} MB / {facts.archive_bytes / MB:.1f} MB)",
            )
        )
        if facts.parquet_bytes is not None:
            frac = facts.parquet_bytes / facts.raw_bytes
            rows.append(
                Row(
                    "Reference store on disk",
                    f"{B * frac / GB:.1f} GB",
                    "extrapolated",
                    f"{B / GB:.1f} GB x {frac:.4f} (Parquet / raw on {facts.name}: "
                    f"{facts.parquet_bytes / MB:.1f} MB / {facts.raw_bytes / MB:.1f} MB)",
                )
            )
    else:
        rows.append(Row("Archive and reference store on disk", "-", "not measured", "otb ref build CORPUS"))
    for stack in stacks:
        d = disks.get(stack)
        if d is None or not d.get("raw_bytes_sent"):
            rows.append(
                Row(f"{stack} on disk", "-", "not measured", "after a replay: tests/scale/stack_disk.py --session ID")
            )
            continue
        ratio = d["bytes"] / d["raw_bytes_sent"]
        rows.append(
            Row(
                f"{stack} on disk",
                f"{B * ratio / GB:.1f} GB",
                "extrapolated",
                f"{B / GB:.1f} GB x {ratio:.4f} (volumes / raw sent: {d['bytes'] / MB:.1f} MB / "
                f"{d['raw_bytes_sent'] / MB:.1f} MB; {d['source']})",
            )
        )
    return rows


# ---------- rendering ----------


def render(facts: CorpusFacts, load: ScenarioLoad, measures: list[Measure], rows: list[Row], notes: list[str]) -> str:
    lines = [
        f"# Scale report: {load.id} on {facts.name}",
        "",
        f"Corpus `{facts.name}` ({facts.corpus_id[:12]}), {facts.duration_s / 3600:.2f} h of recording, "
        f"{facts.archive_bytes / MB:.1f} MB archived"
        + (f", {facts.raw_bytes / MB:.1f} MB raw OTLP/JSON." if facts.raw_bytes else ", raw size unknown."),
        "",
        "## Measured",
        "",
        "Paced rows ran at their input's rate (a capture, a 1x replay): their MB/s is what was needed, not what the "
        "component can do. MB per CPU second is what one busy core would do.",
        "",
        "| Component | What | MB/s | MB per CPU s | Peak RSS MB | Driven | Host | Source |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for m in measures:
        lines.append(
            f"| {m.component} | {m.what} | {_num(m.raw_mb_per_s)} | {_num(m.raw_mb_per_cpu_s)} | "
            f"{_num(m.peak_rss_mb)} | {'capacity' if m.capacity else 'paced'} | {_host(m)} | `{m.source}` |"
        )
    if not measures:
        lines.append("| (none) | | | | | | | |")
    lines += [
        "",
        f"## Arithmetic for {load.id}",
        "",
        "| Quantity | Value | Basis | How |",
        "|---|---|---|---|",
    ]
    lines += [f"| {r.quantity} | {r.value} | {r.basis} | {r.how} |" for r in rows]
    if notes:
        lines += ["", "## Gaps in the evidence", "", *(f"- {n}" for n in notes)]
    return "\n".join(lines) + "\n"


def _num(v: float | None) -> str:
    return "-" if v is None else f"{v:g}"


def scale_report(
    corpus: Path,
    *,
    session_dir: Path | None,
    benches: list[Path],
    scenario: Path = DEFAULT_SCENARIO,
    stacks: list[str] | None = None,
    workers: tuple[int, ...] = (4, 16, 32),
    out: Path | None = None,
) -> dict:
    """Writes scale-report.md and scale-report.json to out (default CORPUS/derived/scale) and returns the JSON."""
    facts = corpus_facts(corpus)
    gateway = gateway_measures(corpus)
    measures = gateway + ref_measures(corpus)
    disks: dict[str, dict] = {}
    notes = [] if gateway else ["no gateway report under derived/gateway: the corpus was not captured by otb capture"]
    session_stacks: list[str] = []
    if session_dir is not None:
        got, disks, missing = session_measures(session_dir)
        measures += got
        notes += missing
        session_stacks = json.loads((session_dir / "session.json").read_text())["stacks"]
    scale_dir = corpus / "derived" / "scale"
    found = sorted(p for p in scale_dir.glob("*.json") if p.name != "scale-report.json") if scale_dir.exists() else []
    for b in [*found, *benches]:
        measures += bench_measures(b)
    load = scenario_load(scenario)
    stacks = stacks or session_stacks or sorted(registry())
    rows = arithmetic(load, facts, measures, disks, stacks, workers)
    doc = {
        "corpus": asdict(facts),
        "scenario": asdict(load),
        "session": None if session_dir is None else session_dir.name,
        "measured": [asdict(m) for m in measures],
        "arithmetic": [asdict(r) for r in rows],
        "notes": notes,
    }
    out = out or scale_dir
    out.mkdir(parents=True, exist_ok=True)
    (out / "scale-report.json").write_text(json.dumps(doc, indent=1) + "\n")
    (out / "scale-report.md").write_text(render(facts, load, measures, rows, notes))
    return doc


@command("scale-report")
def cmd_scale_report(argv: list[str]) -> int:
    from otb.ingest import Session, sessions_dir

    p = argparse.ArgumentParser(prog="otb scale-report", description=__doc__.split("\n\n")[0])
    p.add_argument("corpus", type=Path)
    p.add_argument("--session", default=None, help="ingest session id (default: the newest on this corpus)")
    p.add_argument("--repo", type=Path, default=REPO, help="where state/sessions lives")
    p.add_argument("--scenario", type=Path, default=DEFAULT_SCENARIO)
    p.add_argument("--stacks", default=None, help="comma-separated (default: the session's)")
    p.add_argument("--workers", default="4,16,32", help="reference build worker counts to project")
    p.add_argument("--bench", type=Path, action="append", default=[], help="a tests/scale output (repeatable)")
    p.add_argument("--out", type=Path, default=None, help="default: CORPUS/derived/scale")
    a = p.parse_args(argv)
    from otb.archive import Archive

    corpus_id = Archive(a.corpus).manifest.corpus_id
    root = sessions_dir(a.repo)
    session_dir = None
    if a.session:
        session_dir = root / a.session
        if not (session_dir / "session.json").exists():
            p.error(f"no session {a.session} under {root}")
    elif root.exists():
        mine = [d for d in sorted(root.iterdir()) if (d / "session.json").exists()]
        mine = [d for d in mine if Session.load(d / "session.json").corpus_id == corpus_id]
        session_dir = mine[-1] if mine else None
    doc = scale_report(
        a.corpus,
        session_dir=session_dir,
        benches=a.bench,
        scenario=a.scenario,
        stacks=a.stacks.split(",") if a.stacks else None,
        workers=tuple(int(w) for w in a.workers.split(",")),
        out=a.out,
    )
    print((a.out or a.corpus / "derived" / "scale").joinpath("scale-report.md").read_text())
    print(f"session: {doc['session']}")
    return 0
