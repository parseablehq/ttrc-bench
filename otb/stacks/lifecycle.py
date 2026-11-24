"""Stack lifecycle and its CLI verbs: `otb bootstrap` (images or charts, MCP server environments), `otb up`,
`otb down`, `otb doctor`. Doctor enforces pins and equal budgets, not just records them. The runtime
(`--runtime`, OTB_RUNTIME) picks compose (here) or Kubernetes (otb.stacks.k8s) for starting and inspecting the
containers; readiness and provisioning are HTTP and shared."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Literal, cast

import yaml

from otb.cli import COMMANDS
from otb.stacks import k8s
from otb.stacks.context import StackContext, basic
from otb.stacks.definition import (
    K8S_PROFILES,
    REPO,
    RUNTIMES,
    Runtime,
    StackDef,
    StackDefError,
    StdioLaunch,
    registry,
    runtime_from_env,
    select,
)
from otb.stacks.secrets import ensure_secrets, secrets_dir

PROJECT = "otb"
Profile = Literal["laptop", "bighost"]
PROFILES: tuple[Profile, ...] = ("laptop", "bighost")
RUNTIME_PROFILES: dict[Runtime, tuple[str, ...]] = {"compose": PROFILES, "k8s": K8S_PROFILES}


def _runtime(stacks: list[StackDef]) -> Runtime:
    runtimes = {s.runtime for s in stacks}
    if len(runtimes) != 1:
        raise StackDefError(f"stacks loaded under different runtimes: {sorted(runtimes)}")
    return runtimes.pop()


def compose_argv(stacks: list[StackDef], profile: str, repo: Path = REPO) -> list[str]:
    argv = ["docker", "compose", "-p", PROJECT, "--project-directory", str(repo)]
    argv += ["--env-file", str(repo / "profiles" / f"{profile}.env")]
    argv += ["--env-file", str(secrets_dir(repo) / "all.env")]
    argv += ["-f", str(repo / "compose" / "base.yaml")]
    for s in stacks:
        argv += ["-f", str(s.compose_file)]
    return argv


def _run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, text=True, check=True, **kw)  # type: ignore[call-overload]


def wait_ready(stack: StackDef, ctx: StackContext) -> float:
    """Seconds until every readiness URL answers its expected status."""
    t0 = time.monotonic()
    for r in stack.readiness:
        headers = basic(ctx.secret(r.basic_auth[0]), ctx.secret(r.basic_auth[1])) if r.basic_auth else {}
        last = "no response"
        while True:
            try:
                resp = ctx.http("GET", r.url, headers=headers, timeout_s=5)
                if resp.status == r.expect_status:
                    break
                last = f"HTTP {resp.status}"
            except OSError as e:
                last = str(e)
            if time.monotonic() - t0 > r.timeout_s:
                raise TimeoutError(f"{stack.id}: readiness {r.name} ({r.url}) not ready after {r.timeout_s}s: {last}")
            time.sleep(2)
    ctx.exchanges.clear()
    return time.monotonic() - t0


def state_dir(repo: Path = REPO) -> Path:
    return repo / ".otb" / "state"


def provision(stack: StackDef, ctx: StackContext, repo: Path = REPO) -> list[str]:
    """Run every post_up step. Each step checks the live system first and does nothing when it already holds
    (so a wiped volume is re-provisioned and a second `otb up` is a no-op); the marker records when it last ran."""
    steps = stack.probes.provisioning()
    ran = []
    for name in stack.post_up:
        changed = steps[name](ctx)
        marker = state_dir(repo) / stack.id / f"{name}.json"
        if changed or not marker.exists():
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(
                json.dumps(
                    {"step": name, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "changed": bool(changed)}
                )
                + "\n"
            )
        if changed:
            ran.append(name)
    return ran


def container_ids(stacks: list[StackDef], profile: str, repo: Path = REPO) -> dict[str, str]:
    out = _run([*compose_argv(stacks, profile, repo), "ps", "-a", "--format", "json"], capture_output=True).stdout
    rows = [json.loads(line) for line in out.splitlines() if line.strip()]
    return {r["Service"]: f"{r['ID']} {r['State']}" for r in rows}


def up(stacks: list[StackDef], profile: str, repo: Path = REPO) -> dict[str, object]:
    runtime = _runtime(stacks)
    secrets = ensure_secrets(list(registry(repo, runtime).values()), repo)
    t0 = time.monotonic()
    report: dict[str, object] = {"runtime": runtime, "profile": profile, "stacks": {}}
    if runtime == "k8s":
        report["releases"] = k8s.apply(stacks, profile, secrets, repo)
    else:
        before = container_ids(stacks, profile, repo)
        _run([*compose_argv(stacks, profile, repo), "up", "-d"])
    for s in stacks:
        ctx = StackContext(s, repo)
        ready_s = wait_ready(s, ctx)
        ran = provision(s, ctx, repo)
        cast(dict[str, object], report["stacks"])[s.id] = {
            "ready_after_s": round(time.monotonic() - t0, 1),
            "readiness_wait_s": round(ready_s, 1),
            "provisioned": ran,
        }
    if runtime == "compose":
        after = container_ids(stacks, profile, repo)
        report["containers_changed"] = sorted(k for k in after if before.get(k) != after[k])
    report["total_s"] = round(time.monotonic() - t0, 1)
    return report


def down(stacks: list[StackDef], profile: str, volumes: bool, repo: Path = REPO) -> None:
    runtime = _runtime(stacks)
    every = registry(repo, runtime)
    ensure_secrets(list(every.values()), repo)
    if runtime == "k8s":
        k8s.remove(stacks, profile, volumes, repo)
    else:
        orphans = ["--remove-orphans"] if {s.id for s in stacks} == set(every) else []
        _run([*compose_argv(stacks, profile, repo), "down"] + orphans + (["--volumes"] if volumes else []))
    if volumes:
        from otb.ingest import record_wipe

        for s in stacks:
            shutil.rmtree(state_dir(repo) / s.id, ignore_errors=True)
            record_wipe(s.id, repo)


def bootstrap(stacks: list[StackDef], profile: str, repo: Path = REPO) -> None:
    """Pull every image by digest (compose) or every pinned chart (k8s), and build each MCP server from its
    lockfile into .otb/."""
    runtime = _runtime(stacks)
    ensure_secrets(list(registry(repo, runtime).values()), repo)
    if runtime == "k8s":
        k8s.pull_charts(stacks, repo)
        k8s.install_post_renderer(repo)
    else:
        _run([*compose_argv(stacks, profile, repo), "pull", "--quiet"])
    for s in stacks:
        launch = s.surface.mcp
        lock = repo / launch.lock if isinstance(launch, StdioLaunch) and launch.lock else None
        if isinstance(launch, StdioLaunch) and launch.kind == "docker":
            if runtime == "k8s":
                raise StackDefError(f"{s.id}: MCP kind docker needs a Docker daemon; the k8s runtime has none")
            image = next(a for a in launch.argv if "@sha256:" in a)
            _run(["docker", "pull", "--quiet", image])
        elif lock is not None and launch.kind == "binary":
            install_binary(lock, repo / ".otb" / "bin")
        elif lock is not None and launch.kind == "venv":
            venv = repo / ".otb" / "venvs" / lock.parent.name
            _run(["uv", "venv", "--allow-existing", "--python", "3.13", str(venv)])
            _run(
                [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    str(venv / "bin" / "python"),
                    "--require-hashes",
                    "-r",
                    str(lock),
                ]
            )
        elif lock is not None and launch.kind == "node":
            target = repo / ".otb" / "node" / lock.parent.name
            target.mkdir(parents=True, exist_ok=True)
            for f in ("package.json", "package-lock.json"):
                shutil.copyfile(lock.parent / f, target / f)
            _run(["npm", "ci", "--no-audit", "--no-fund", "--prefix", str(target)])


_ARCH = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}


def host_platform() -> str:
    return f"{platform.system().lower()}-{_ARCH.get(platform.machine().lower(), platform.machine().lower())}"


def install_binary(lock: Path, bin_dir: Path, host: str | None = None) -> Path:
    """Install the release binary a `kind: binary` lockfile pins for this host into bin_dir/<name>. The lockfile
    names, per `<os>-<arch>`, the archive URL and its sha256; the archive is refused and discarded on a mismatch.
    A binary already installed from the same archive is kept."""
    spec = yaml.safe_load(lock.read_text())
    host = host or host_platform()
    assets = spec["assets"]
    if host not in assets:
        raise StackDefError(f"{lock}: no asset for {host}; known: {', '.join(sorted(assets))}")
    url, want = assets[host]["url"], assets[host]["sha256"]
    name, member = spec["name"], spec.get("binary", spec["name"])
    target, stamp = bin_dir / name, bin_dir / f"{name}.sha256"
    if target.exists() and stamp.exists() and stamp.read_text().strip() == want:
        return target
    bin_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=bin_dir) as tmp:
        archive = Path(tmp) / "archive"
        with urllib.request.urlopen(url, timeout=120) as r, archive.open("wb") as f:
            shutil.copyfileobj(r, f)
        got = _file_sha(archive)
        if got != want:
            raise StackDefError(f"{lock}: {host} archive sha256 {got} differs from the lock's {want} ({url})")
        with tarfile.open(archive) as tar:
            src = tar.extractfile(member)
            if src is None:
                raise StackDefError(f"{lock}: {member} is not a file in the {host} archive")
            staged = Path(tmp) / name
            with staged.open("wb") as out:
                shutil.copyfileobj(src, out)
        staged.chmod(0o755)
        staged.replace(target)
    stamp.write_text(want + "\n")
    return target


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _mem_bytes(v: object) -> int:
    if isinstance(v, int):
        return v
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kmgt]?)i?b?", str(v).strip().lower())
    if not m:
        raise ValueError(f"unparseable memory {v!r}")
    return int(float(m.group(1)) * 1024 ** "_kmgt".index(m.group(2) or "_"))


def doctor(stacks: list[StackDef], profiles: list[str], repo: Path = REPO) -> list[str]:
    runtime = _runtime(stacks)
    ensure_secrets(list(registry(repo, runtime).values()), repo)
    problems: list[str] = []
    launches = [s.surface.mcp for s in stacks]
    for lock in ["uv.lock"] + [m.lock for m in launches if isinstance(m, StdioLaunch) and m.lock]:
        if not (repo / lock).exists():
            problems.append(f"lockfile missing: {lock}")
    for s in stacks:
        launch = s.surface.mcp
        if isinstance(launch, StdioLaunch) and launch.kind == "docker":
            if runtime == "k8s":
                problems.append(f"{s.id}: MCP kind docker needs a Docker daemon; the k8s runtime has none")
            images = [a for a in launch.argv if "@sha256:" in a or re.match(r"^[\w./-]+:[\w.-]+$", a)]
            if not images or not k8s.PINNED_IMAGE.match(images[0]) or images[0] != launch.pin:
                problems.append(f"{s.id}: MCP image not pinned by digest or differs from mcp.pin: {images or '-'}")
        elif isinstance(launch, StdioLaunch) and launch.lock and (repo / launch.lock).exists():
            want = f"{launch.lock}@sha256:{_file_sha(repo / launch.lock)}"
            if launch.pin != want:
                problems.append(f"{s.id}: mcp.pin {launch.pin!r} does not match lockfile ({want})")
    if runtime == "k8s":
        return problems + k8s.doctor(stacks, profiles, repo)
    for profile in profiles:
        out = _run([*compose_argv(stacks, profile, repo), "config", "--format", "json"], capture_output=True).stdout
        services = cast(dict[str, dict[str, object]], json.loads(out)["services"])
        budgets: dict[str, dict[str, tuple[float, int]]] = {}
        stack_ids = {s.id for s in stacks}
        for name, svc in sorted(services.items()):
            image = str(svc.get("image", ""))
            if not k8s.PINNED_IMAGE.match(image):
                problems.append(f"{profile}: service {name}: image {image!r} is not name:tag@sha256:<digest>")
            labels = cast(dict[str, str], svc.get("labels") or {})
            sid, role = labels.get("otb.stack"), labels.get("otb.role")
            if sid not in stack_ids or role not in ("backend", "ingest", "setup"):
                problems.append(f"{profile}: service {name}: labels otb.stack/otb.role missing or invalid")
                continue
            if role == "setup":
                continue
            cpus, mem = svc.get("cpus"), svc.get("mem_limit")
            if cpus is None or mem is None:
                problems.append(f"{profile}: service {name}: cpus and mem_limit must be set")
                continue
            c, m = budgets.setdefault(role, {}).get(sid, (0.0, 0))
            budgets[role][sid] = (round(c + float(cast(float, cpus)), 3), m + _mem_bytes(mem))
        for role, per_stack in sorted(budgets.items()):
            if len(set(per_stack.values())) > 1:
                detail = ", ".join(f"{k}={c} CPU/{m / 2**30:.2f} GiB" for k, (c, m) in sorted(per_stack.items()))
                problems.append(f"{profile}: unequal {role} budgets: {detail}")
            missing = stack_ids - set(per_stack)
            if missing:
                problems.append(f"{profile}: no {role} services for {sorted(missing)}")
    return problems


def budgets_table(stacks: list[StackDef], profile: str, repo: Path = REPO) -> dict[str, dict[str, object]]:
    if _runtime(stacks) == "k8s":
        return k8s.budgets_table(stacks, profile, repo)
    out = _run([*compose_argv(stacks, profile, repo), "config", "--format", "json"], capture_output=True).stdout
    table: dict[str, dict[str, object]] = {}
    for svc in json.loads(out)["services"].values():
        labels = svc.get("labels") or {}
        if labels.get("otb.role") in ("backend", "ingest"):
            row = table.setdefault(f"{labels['otb.stack']}/{labels['otb.role']}", {"cpus": 0.0, "mem_bytes": 0})
            row["cpus"] = round(cast(float, row["cpus"]) + float(svc["cpus"]), 3)
            row["mem_bytes"] = cast(int, row["mem_bytes"]) + _mem_bytes(svc["mem_limit"])
    return table


def _args(argv: list[str], *, volumes: bool = False, all_profiles: bool = False) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--runtime", choices=RUNTIMES, default=runtime_from_env(), help="default: OTB_RUNTIME or compose")
    p.add_argument("--profile", default=None, help="compose: laptop (default) or bighost; k8s: kind (default), gke or gke-pilot")
    p.add_argument("--stacks", default=None, help="comma-separated, e.g. b1,b4 (default: all)")
    if volumes:
        p.add_argument("--volumes", action="store_true")
    a = p.parse_args(argv)
    allowed = RUNTIME_PROFILES[a.runtime]
    if a.profile is None and not all_profiles:
        a.profile = allowed[0]
    if a.profile is not None and a.profile not in allowed:
        p.error(f"--profile {a.profile}: the {a.runtime} runtime knows {', '.join(allowed)}")
    return a


def cmd_up(argv: list[str]) -> int:
    a = _args(argv)
    print(json.dumps(up(select(registry(runtime=a.runtime), a.stacks), a.profile), indent=1))
    return 0


def cmd_down(argv: list[str]) -> int:
    a = _args(argv, volumes=True)
    down(select(registry(runtime=a.runtime), a.stacks), a.profile, a.volumes)
    return 0


def cmd_bootstrap(argv: list[str]) -> int:
    a = _args(argv)
    bootstrap(select(registry(runtime=a.runtime), a.stacks), a.profile)
    return 0


def cmd_doctor(argv: list[str]) -> int:
    a = _args(argv, all_profiles=True)
    stacks = select(registry(runtime=a.runtime), a.stacks)
    profiles = [a.profile] if a.profile else list(RUNTIME_PROFILES[a.runtime])
    problems = doctor(stacks, profiles)
    for profile in profiles:
        try:
            table = budgets_table(stacks, profile)
        except (StackDefError, subprocess.CalledProcessError) as e:
            print(f"{profile:8s} budgets not renderable: {e}")
            continue
        for k, v in sorted(table.items()):
            print(f"{profile:8s} {k:28s} {v['cpus']:>5} CPU {cast(int, v['mem_bytes']) / 2**30:6.2f} GiB")
    for p in problems:
        print(f"FAIL {p}")
    print("doctor: ok" if not problems else f"doctor: {len(problems)} problem(s)")
    return 1 if problems else 0


COMMANDS.update({"up": cmd_up, "down": cmd_down, "bootstrap": cmd_bootstrap, "doctor": cmd_doctor})
