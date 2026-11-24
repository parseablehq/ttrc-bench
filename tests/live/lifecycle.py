"""Checks 1, 2 and 6 of U4, each appending its record to docs/v1/units/U4-lifecycle.json.

  uv run python tests/live/lifecycle.py doctor   # doctor passes; fails on a bare tag and on unequal budgets
  uv run python tests/live/lifecycle.py up       # from empty: up, readiness, provisioning; a second up is a no-op
  uv run python tests/live/lifecycle.py down     # down --volumes leaves no otb container or volume

The doctor negatives run against a temporary copy of the repository; the committed files are never edited.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from otb.stacks import REPO, registry
from otb.stacks.lifecycle import doctor, down, up

OUT = REPO / "docs" / "v1" / "units" / "U4-lifecycle.json"
COPY = ["compose", "profiles", "stacks", "servers", "uv.lock"]


def record(key: str, value: object) -> None:
    data = json.loads(OUT.read_text()) if OUT.exists() else {"unit": "U4 checks 1, 2, 6"}
    data[key] = value
    OUT.write_text(json.dumps(data, indent=1) + "\n")


def _doctor_on_copy(edit: tuple[str, str, str]) -> list[str]:
    """Doctor's problems for a temporary repo copy with one textual edit (file, old, new)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for name in COPY:
            src = REPO / name
            (shutil.copytree if src.is_dir() else shutil.copyfile)(src, root / name)
        path, old, new = edit
        text = (root / path).read_text()
        assert old in text, f"{path}: {old!r} not found"
        (root / path).write_text(text.replace(old, new, 1))
        return doctor(list(registry(root).values()), ["laptop"], root)


def cmd_doctor() -> bool:
    ok_problems = doctor(list(registry().values()), ["laptop", "bighost"])
    compose = (REPO / "stacks/b3-clickhouse/compose.yaml").read_text()
    pinned = re.search(r"image: (clickhouse/clickhouse-server:[\w.]+)(@sha256:[0-9a-f]{64})", compose)
    assert pinned
    bare = _doctor_on_copy(("stacks/b3-clickhouse/compose.yaml", pinned.group(0), f"image: {pinned.group(1)}"))
    unequal = _doctor_on_copy(("profiles/laptop.env", "B4_PARSEABLE_MEM=2560m", "B4_PARSEABLE_MEM=3072m"))
    result = {
        "committed_tree": {"problems": ok_problems, "pass": not ok_problems},
        "bare_tag": {
            "edit": f"{pinned.group(0)} -> image: {pinned.group(1)}",
            "problems": bare,
            "pass": any(pinned.group(1) in p for p in bare),
        },
        "unequal_budget": {
            "edit": "B4_PARSEABLE_MEM=2560m -> 3072m",
            "problems": unequal,
            "pass": any("unequal backend budgets" in p for p in unequal),
        },
    }
    record("doctor", result)
    print(json.dumps(result, indent=1))
    return all(v["pass"] for v in result.values())


def _otb_resources() -> dict[str, list[str]]:
    def lines(argv: list[str]) -> list[str]:
        return sorted(x for x in subprocess.run(argv, capture_output=True, text=True, check=True).stdout.split() if x)

    label = "label=com.docker.compose.project=otb"
    return {
        "containers": lines(["docker", "ps", "-a", "--filter", label, "--format", "{{.Names}}"]),
        "volumes": lines(["docker", "volume", "ls", "--filter", label, "--format", "{{.Name}}"]),
    }


def _memory_by_stack() -> dict[str, float]:
    out = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.MemUsage}}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    labels = {}
    for name in _otb_resources()["containers"]:
        insp = subprocess.run(
            ["docker", "inspect", name, "--format", '{{index .Config.Labels "otb.stack"}}'],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        labels[name] = insp
    mib: dict[str, float] = {}
    unit = {"KiB": 1 / 1024, "MiB": 1.0, "GiB": 1024.0, "B": 1 / 2**20}
    for line in out.splitlines():
        name, usage = line.split("\t")
        if name in labels:
            m = re.match(r"([\d.]+)(\w+)", usage)
            assert m
            mib[labels[name]] = round(mib.get(labels[name], 0.0) + float(m.group(1)) * unit[m.group(2)], 1)
    return mib


def cmd_up() -> bool:
    stacks = list(registry().values())
    before = _otb_resources()
    t0 = time.monotonic()
    first = up(stacks, "laptop")
    first_s = round(time.monotonic() - t0, 1)
    second = up(stacks, "laptop")
    memory = _memory_by_stack()
    result = {
        "started_empty": not before["containers"] and not before["volumes"],
        "first_up": first | {"wall_s": first_s},
        "second_up": second,
        "second_up_is_noop": not second["containers_changed"]
        and not any(s["provisioned"] for s in second["stacks"].values()),  # type: ignore[union-attr]
        "docker_stats_mib_by_stack": memory,
        "docker_stats_mib_total": round(sum(memory.values()), 1),
    }
    record("up", result)
    print(json.dumps(result, indent=1))
    return bool(result["second_up_is_noop"])


def cmd_down() -> bool:
    down(list(registry().values()), "laptop", volumes=True)
    left = _otb_resources()
    result = {"left_after_down_volumes": left, "pass": not left["containers"] and not left["volumes"]}
    record("down", result)
    print(json.dumps(result, indent=1))
    return bool(result["pass"])


if __name__ == "__main__":
    ok = {"doctor": cmd_doctor, "up": cmd_up, "down": cmd_down}[sys.argv[1]]()
    sys.exit(0 if ok else 1)
