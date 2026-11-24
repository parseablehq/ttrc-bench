"""`otb scale-report` over evidence produced here: a reference build of corpus-mini, an ingest session with one
feeder process per stack against stand-in collectors, the U9a gateway benchmark and one stack's disk file.

Ways the report could mislead, each asserted below:
- a paced number (a 1x replay, a live capture) is shown as capacity, or an extrapolation is marked measured;
- a row's arithmetic uses other bytes than `otb scale-report`'s scenario says (week-ladder: 1054.9 GB, 4.61 MB/s
  on its 40-user day), or ratios other than the corpus's own raw, archive and Parquet bytes;
- a measured number is shown without its host, or with a host it did not run on;
- missing evidence (no gateway report, a stack without a disk file, a reference build that misses a segment) is
  left out instead of shown as not measured;
- benchmark files under CORPUS/derived/scale are not found.

Evidence: tests/e2e/artifacts/scale_report.json (the deterministic rows).
"""

import json
import shutil
import time
from pathlib import Path

import pytest
from conftest import ok, otb, write_artifact
from fake_collector import serve, target

from otb.archive import Archive
from otb.host import describe
from otb.ingest import feeder_stats_path, run_session, selected_frames, start_session
from otb.timebase import NS

REPO = Path(__file__).resolve().parents[2]
MINI = REPO / "tests/fixtures/corpus-mini"
PEAK_MB_S = 23.05 * 12 * 1440 / 86400  # week-ladder's 40-user day: MB/min/instance x instances x minutes / s
TOTAL_GB = 1054.9


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


U9A = REPO / "docs/v1/units/U9a-throughput.json"


def rows(doc: dict) -> dict[str, dict]:
    return {r["quantity"]: r for r in doc["arithmetic"]}


@pytest.mark.skipif(not U9A.exists(), reason="the U9a gateway benchmark record is not part of this repository")
def test_scale_report_from_recorded_evidence(tmp_path: Path, artifacts: Path) -> None:
    corpus = tmp_path / "corpus-mini"
    shutil.copytree(MINI, corpus)
    ok(otb("ref", "build", corpus, "--workers", "2"))
    archive = Archive(corpus)
    raw = sum(len(body) for seg in archive.segments() for _, body in archive.bodies(seg))
    archived = sum((corpus / s.path).stat().st_size for s in archive.segments())
    parquet = sum(p.stat().st_size for p in (corpus / "derived/ref").glob("*/*.parquet"))

    first_due = min(e.max_event_ns for _, e in selected_frames(archive, None))
    session, _ = start_session(corpus, ("a", "b"), "replay", lead_s=0, repo=tmp_path, now_ns=first_due - 5 * 86400 * NS)
    ports = {"a": free_port(), "b": free_port()}
    servers = [serve(p, tmp_path / f"{n}.log", set(), 0, 0.0) for n, p in ports.items()]
    run_session(session, [target(n, f"http://127.0.0.1:{p}") for n, p in ports.items()], tmp_path, log=print)
    for s in servers:
        s.shutdown()
    sent_a = sum(json.loads(x)["raw_bytes"] for x in feeder_stats_path(session, "a", tmp_path).read_text().splitlines())
    disk = session.dir(tmp_path) / "ingest" / "a.disk.json"
    disk.write_text(json.dumps({"stack": "a", "bytes": 3 * sent_a, "raw_bytes_sent": sent_a, "host": describe()}))
    (corpus / "derived/scale").mkdir()
    shutil.copy(U9A, corpus / "derived/scale/gateway_throughput.json")

    t0 = time.monotonic()
    out = ok(otb("scale-report", corpus, "--repo", tmp_path, "--session", session.id, "--workers", "2,8"))
    doc = json.loads((corpus / "derived/scale/scale-report.json").read_text())
    md = (corpus / "derived/scale/scale-report.md").read_text()
    got = rows(doc)
    feeders = [m for m in doc["measured"] if m["component"] == "feeder"]
    gateway = [m for m in doc["measured"] if m["component"] == "gateway"]
    ref = [m for m in doc["measured"] if m["component"] == "reference build"]
    result = {
        "scenario_gb": round(doc["scenario"]["total_bytes"] / 1e9, 1),
        "peak_mb_s": round(doc["scenario"]["peak_bytes_per_s"] / 1e6, 2),
        "gateway_headroom": [got["Gateway headroom at the peak, one stream"][k] for k in ("value", "basis")],
        "gateway_rows_capacity_with_host": [(m["capacity"], m["host"]["cpus"]) for m in gateway],
        "feeders": sorted((m["what"].split(",")[0], m["capacity"], m["host"] == describe()) for m in feeders),
        "replay_headroom_basis": got["1x replay headroom per stack (one feeder process each)"]["basis"],
        "ref_rows": [(q, r["value"], r["basis"]) for q, r in got.items() if q.startswith("Reference build time")],
        "ref_measure_parallel_and_host": [(m["parallel"], m["host"] == describe()) for m in ref],
        "archive_on_disk": [got["Archive on disk"][k] for k in ("value", "basis")],
        "reference_on_disk": [got["Reference store on disk"][k] for k in ("value", "basis")],
        "stack_a_on_disk": [got["a on disk"][k] for k in ("value", "basis")],
        "stack_b_on_disk": [got["b on disk"][k] for k in ("value", "basis")],
        "follow_headroom": got["Follow-ingest headroom per stack during capture"]["basis"],
        "notes": doc["notes"],
        "md_has_gateway_row": f"| Gateway headroom at the peak, one stream | {127.4 / PEAK_MB_S:.1f}x | measured |"
        in md,
        "stdout_is_md": out.stdout.startswith("# Scale report: week-ladder on corpus-mini"),
    }
    write_artifact(
        artifacts / "scale_report.json",
        {k: v for k, v in result.items() if k != "ref_rows"} | {"ref_rows": [(q, b) for q, _, b in result["ref_rows"]]},
        timing={"scale_report_s": round(time.monotonic() - t0, 2), "ref_rows": result["ref_rows"]},
    )

    ref_rate, b_gb = ref[0]["raw_mb_per_s"], doc["scenario"]["total_bytes"] / 1e9
    assert result["scenario_gb"] == TOTAL_GB and result["peak_mb_s"] == round(PEAK_MB_S, 2)
    assert result["gateway_headroom"] == [f"{127.4 / PEAK_MB_S:.1f}x", "measured"]
    assert result["gateway_rows_capacity_with_host"] == [(True, 12)] * 3
    assert result["feeders"] == [("a", False, True), ("b", False, True)]
    assert result["replay_headroom_basis"] == "extrapolated"
    assert result["ref_rows"] == [
        ("Reference build time, 2 workers", f"{b_gb * 1e3 / ref_rate / 3600:.2f} h", "extrapolated"),
        ("Reference build time, 8 workers", f"{b_gb * 1e3 / (ref_rate * 4) / 3600:.2f} h", "extrapolated"),
    ]
    assert result["ref_measure_parallel_and_host"] == [(2, True)]
    assert result["archive_on_disk"] == [f"{b_gb / (raw / archived):.1f} GB", "extrapolated"]
    assert result["reference_on_disk"] == [f"{b_gb * parquet / raw:.1f} GB", "extrapolated"]
    assert result["stack_a_on_disk"] == [f"{b_gb * 3:.1f} GB", "extrapolated"]
    assert result["stack_b_on_disk"] == ["-", "not measured"]
    assert result["follow_headroom"] == "not measured"
    assert result["notes"] == ["no gateway report under derived/gateway: the corpus was not captured by otb capture"]
    assert result["md_has_gateway_row"] and result["stdout_is_md"]

    built = corpus / "derived/ref/built.jsonl"
    built.write_text("".join(built.read_text().splitlines(keepends=True)[1:]))
    ok(otb("scale-report", corpus, "--repo", tmp_path, "--session", session.id))
    partial = rows(json.loads((corpus / "derived/scale/scale-report.json").read_text()))
    assert partial["Archive and reference store on disk"]["basis"] == "not measured"
    assert "Archive on disk" not in partial


def test_scenario_load_counts_the_background() -> None:
    """The 1 TB arithmetic: tb-hybrid's demo bytes plus its background for the whole day, and the peak rate with
    the background on top of the busiest load step."""
    from otb.scale import scenario_load

    load = scenario_load(REPO / "scenarios/tb-hybrid.yaml")
    assert round(load.total_bytes / 1e12, 3) == 1.0
    assert 11.33 < load.peak_bytes_per_s / 1e6 < 11.6, "the busiest step is the demo at 8 users plus 11.33 MB/s"
    smoke = scenario_load(REPO / "scenarios/k8s-smoke.yaml")
    assert smoke.total_bytes < 1e9, "no background: the demo alone"
