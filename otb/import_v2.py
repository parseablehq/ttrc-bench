"""`otb corpus import-v2`: a harness v2 recording (corpus.yaml + recording/<signal>.jsonl, one OTLP/JSON export
request per line) becomes an otb-archive/1 corpus with the same frames, through the same normaliser as the
gateway.

Resumable. Every raw line becomes exactly one frame (no dedupe, so v2's counts carry over), so after a kill the
number of frames already archived for a signal is the number of lines consumed. The rerun skips those lines,
checks that the last skipped line normalises to the last archived frame's body, and appends the rest. Rotation
is by size only, so segment boundaries, and therefore corpus_id, are the same as an uninterrupted import's.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

import yaml

from otb.archive import SIGNALS, ArchiveError, ArchiveWriter, Header, Manifest, NormaliserStamp, SourcePins
from otb.normalise import normalise
from otb.scenario import DEFAULT_CATALOGUE, Catalogue, sha256_bytes, synthesise_v2_schedule

DEMO_REPO = "https://github.com/open-telemetry/opentelemetry-demo"
DEMO_COLLECTOR_IMAGE = (
    "ghcr.io/open-telemetry/opentelemetry-collector-releases/opentelemetry-collector-contrib:0.159.0"
    "@sha256:1f2c54a30e713fac6b3ae77a1ec84010c2007e29ced8ec666214fc2f6739c1cc"
)
V2_SERVICE_IMAGES = {"_all": "ghcr.io/open-telemetry/demo:3.0.0-<service> (v2 recorded the tag only, no digests)"}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _lines(path: Path) -> Iterator[bytes]:
    with path.open("rb") as f:
        for line in f:
            line = line.rstrip(b"\n")
            if line.strip():
                yield line


def _repo_relative(path: Path) -> str:
    p = path.resolve()
    for parent in p.parents:
        if (parent / ".git").exists():
            return str(p.relative_to(parent))
    return p.name


def _demo_commit(v2_root: Path) -> str:
    head = v2_root / "demo" / ".git" / "HEAD"
    if head.exists():
        ref = head.read_text().strip()
        if not ref.startswith("ref:"):
            return ref
    return "unknown (v2 recorded the tag only)"


def header_for(corpus_yaml: Path, cfg: dict, name: str, rotate_bytes: int) -> Header:
    return Header(
        name=name,
        instances=("default",),
        source=SourcePins(
            demo_repo=DEMO_REPO,
            demo_commit=_demo_commit(corpus_yaml.parent),
            demo_tag=str(cfg["source"]["demo_repository_tag"]),
            service_images=V2_SERVICE_IMAGES,
            demo_collector_image=DEMO_COLLECTOR_IMAGE,
        ),
        normaliser=NormaliserStamp.current(),
        rotate_bytes=rotate_bytes,
        imported_from={
            "v2_corpus_yaml": _repo_relative(corpus_yaml),
            "v2_sha256": dict(cfg["sha256"]),
            "v2_window_utc": list(cfg["window_utc"]),
        },
    )


def _write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def import_v2(
    corpus_yaml: Path, out: Path, *, rotate_bytes: int = 64 << 20, catalogue: Path = DEFAULT_CATALOGUE
) -> Manifest:
    """Also writes scenario.yaml and schedule.yaml synthesised from corpus.yaml's events (see
    scenario.synthesise_v2_schedule), and seals with their hashes."""
    cfg = yaml.safe_load(corpus_yaml.read_text())
    files = {s: corpus_yaml.parent / cfg["recording"][s] for s in SIGNALS}
    for s, path in files.items():
        got = _sha256_file(path)
        if got != cfg["sha256"][s]:
            raise ArchiveError(f"{path}: sha256 {got} != corpus.yaml {cfg['sha256'][s]}")
    writer = ArchiveWriter(out, header_for(corpus_yaml, cfg, out.name, rotate_bytes), rotate_s=None, dedupe=False)
    try:
        if writer.sealed:
            return writer.sealed_manifest
        for s in SIGNALS:
            done = writer.frames_written(s)
            last = writer.last_frame(s)
            for i, line in enumerate(_lines(files[s])):
                if i < done:
                    if i == done - 1 and last is not None:
                        body_sha = hashlib.sha256(normalise(s, line).body).hexdigest()
                        if body_sha != last.body_sha256:
                            raise ArchiveError(f"{s}: line {i} does not match the last archived frame; not resuming")
                    continue
                writer.append(s, normalise(s, line), received_ns=0)
        segs = writer.seal_segments()
        start = min(x.min_event_ns for x in segs if x.min_event_ns)
        end = max(x.max_event_ns for x in segs)
        scenario, schedule = synthesise_v2_schedule(cfg, Catalogue.load(catalogue), start, end)
        schedule_yaml = schedule.to_yaml()
        _write(out / "scenario.yaml", scenario)
        _write(out / "schedule.yaml", schedule_yaml)
        return writer.seal(scenario_sha256=sha256_bytes(scenario), schedule_sha256=sha256_bytes(schedule_yaml))
    finally:
        writer.close()
