"""Each stack's volume bytes after an ingest session, for `otb scale-report`'s per-stack disk projection.

  uv run python tests/scale/stack_disk.py [--session ID] [--repo DIR]

Run it on the stack host right after a replay finishes (and before `otb down --volumes`). It reads `docker system df
-v` (read-only), sums each stack's compose volumes (`otb_<bN>-*`), and writes `<stack>.disk.json` into the
session's ingest directory with the raw bytes that stack was sent (from its feeder stats). The ratio of the two is
what scale-report carries to the scenario's bytes. Docker prints sizes to three or four significant figures, so the
ratio is that precise. Stacks keep internal overheads (WAL, indexes, compaction) whose size is not linear in data,
so the projection is a first estimate: repeat it after a larger replay if the ratio moves.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

UNITS = {"B": 1, "kB": 10**3, "MB": 10**6, "GB": 10**9, "TB": 10**12}
SIZE = re.compile(r"([0-9.]+)([A-Za-z]+)")


def parse_size(text: str) -> int:
    """Docker's human sizes are decimal: 1.032kB is 1032 bytes."""
    m = SIZE.fullmatch(text.strip())
    if m is None or m.group(2) not in UNITS:
        raise ValueError(f"unknown size {text!r}")
    return round(float(m.group(1)) * UNITS[m.group(2)])


def volumes_by_stack(df: dict, project: str, stacks: list[str]) -> dict[str, dict[str, int]]:
    """{stack: {volume: bytes}} for the volumes compose created for each stack: `<project>_<bN>-<name>`."""
    out: dict[str, dict[str, int]] = {s: {} for s in stacks}
    for v in df["Volumes"]:
        for s in stacks:
            if v["Name"].startswith(f"{project}_{s.split('-')[0]}-"):
                out[s][v["Name"]] = parse_size(v["Size"])
    return out


def main() -> int:
    from otb.host import describe
    from otb.ingest import Session, feeder_stats_path, latest_session, sessions_dir
    from otb.stacks.lifecycle import PROJECT

    p = argparse.ArgumentParser()
    p.add_argument("--session", default=None, help="session id (default: the newest)")
    p.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    a = p.parse_args()
    s = Session.load(sessions_dir(a.repo) / a.session / "session.json") if a.session else latest_session(a.repo)
    df = json.loads(
        subprocess.run(
            ["docker", "system", "df", "-v", "--format", "{{json .}}"], check=True, capture_output=True, text=True
        ).stdout
    )
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    for stack, vols in volumes_by_stack(df, PROJECT, list(s.stacks)).items():
        stats = feeder_stats_path(s, stack, a.repo)
        sent = sum(json.loads(x)["raw_bytes"] for x in stats.read_text().splitlines()) if stats.exists() else None
        doc = {
            "stack": stack,
            "session": s.id,
            "measured_utc": now,
            "method": "docker system df -v",
            "bytes": sum(vols.values()) if vols else None,
            "volumes": vols,
            "raw_bytes_sent": sent,
            "host": describe(),
        }
        (s.dir(a.repo) / "ingest" / f"{stack}.disk.json").write_text(json.dumps(doc, indent=1) + "\n")
        print(json.dumps(doc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
