"""Refuse a rendered stack set that SPEC 4 rule 1 would refuse under compose. The stacks are rendered the way
`otb doctor --runtime k8s` renders them (otb.stacks.k8s.render: helm template per chart release from the pinned
archive in .otb/charts, the manifests as they are), or read from a file.

    uv run python tests/k8s/check_rendered.py --profile kind --env profiles/laptop.env
    uv run python tests/k8s/check_rendered.py rendered.yaml

What it refuses, each a way the Kubernetes runtime could quietly stop matching compose:
- a pod that is not Guaranteed QoS: a container or init container without cpu and memory limits, or with requests
  that differ from them (Kubernetes defaults absent requests to the limits);
- an image without a digest, one not listed in deploy/k8s/charts.lock.yaml, or one whose repository a
  stacks/*/compose.yaml runs at another digest;
- a pod without otb.stack and otb.role (backend | ingest | setup) labels;
- a backend or ingest container with a livenessProbe: compose restarts a container only when it exits, and a
  probe that restarts a slow backend changes what a stack answers (deploy/k8s/stacks/no-liveness/);
- per-stack backend or ingest totals that differ between stacks, or (with --env) from the compose profile's
  budgets; a pod's total is max(sum of containers, largest init container) times replicas;
- a stack without backend or ingest pods, a Helm test hook, a PVC with storageClassName "" (no provisioner);
- (with --env) a compose setting that does not reach the container running the same image: every compose
  environment variable must be set there to the same value (a credential: from a Secret), and every command flag
  that takes a profile variable must be among its args with the profile's value.

Prints one line per stack and role, then each problem; exits 1 if there is any.
"""

from __future__ import annotations

import argparse
import re
import sys
from decimal import Decimal
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
ROLES = ("backend", "ingest", "setup")
BUDGETED = ("backend", "ingest")
_UNITS = {"": 1, "k": 10**3, "M": 10**6, "G": 10**9, "T": 10**12, "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40}


# Parseable's local-store data directory, which compose sets and an object-store render (gke) does not need
LOCAL_STORE_ONLY = {"P_FS_DIR"}
OBJECT_STORES = {"gcs-store", "s3-store", "blob-store"}


def cpu_milli(v: object) -> int:
    s = str(v)
    return int(Decimal(s[:-1])) if s.endswith("m") else int(Decimal(s) * 1000)


def mem_bytes(v: object) -> int:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([kMGT]i?)?", str(v))
    if not m:
        raise ValueError(f"unparseable memory {v!r}")
    return int(Decimal(m.group(1)) * _UNITS[m.group(2) or ""])


def compose_mem(v: str) -> int:
    """docker's 640m / 2g: binary units."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([kmg])", v.strip().lower())
    if not m:
        raise ValueError(f"unparseable compose memory {v!r}")
    return int(Decimal(m.group(1)) * 1024 ** "_kmg".index(m.group(2)))


def normalise_image(ref: str) -> str:
    for prefix in ("docker.io/library/", "docker.io/"):
        if ref.startswith(prefix):
            return ref[len(prefix) :]
    return ref


def split_image(ref: str) -> tuple[str, str | None]:
    """(repository, sha256 digest or None); the tag is dropped."""
    name, _, digest = ref.partition("@")
    repo = re.sub(r":[^/:]+$", "", name)
    return repo, digest or None


def pod_templates(doc: dict) -> list[tuple[dict, dict, int]]:
    """(pod metadata, pod spec, replicas) of every workload in one rendered object."""
    kind, spec = doc.get("kind"), doc.get("spec") or {}
    if kind == "Pod":
        return [(doc.get("metadata") or {}, spec, 1)]
    if kind in ("Deployment", "StatefulSet", "ReplicaSet"):
        t = spec["template"]
        return [(t.get("metadata") or {}, t["spec"], int(spec.get("replicas", 1)))]
    if kind in ("DaemonSet", "Job"):
        t = spec["template"]
        return [(t.get("metadata") or {}, t["spec"], 1)]
    if kind == "CronJob":
        t = spec["jobTemplate"]["spec"]["template"]
        return [(t.get("metadata") or {}, t["spec"], 1)]
    return []


def container_budget(c: dict, where: str, problems: list[str]) -> tuple[int, int]:
    res = c.get("resources") or {}
    limits, requests = res.get("limits") or {}, res.get("requests") or {}
    if "cpu" not in limits or "memory" not in limits:
        problems.append(f"{where}: cpu and memory limits are required (Guaranteed QoS), got {res or 'none'}")
        return 0, 0
    lim = (cpu_milli(limits["cpu"]), mem_bytes(limits["memory"]))
    req = (cpu_milli(requests.get("cpu", limits["cpu"])), mem_bytes(requests.get("memory", limits["memory"])))
    if req != lim:
        problems.append(f"{where}: requests {requests} differ from limits {limits} (not Guaranteed QoS)")
    return lim


def read_env(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1) for line in path.read_text().splitlines() if line.strip() and not line.startswith("#")
    )


def env_budgets(path: Path) -> dict[str, dict[str, tuple[int, int]]]:
    """The compose profile's per-stack budgets, keyed like the rendered ones (B1_* -> b1-lgtm backend)."""
    env = read_env(path)
    stack_of = {p.parent.name.split("-")[0].upper(): p.parent.name for p in (REPO / "stacks").glob("*/stack.yaml")}
    out: dict[str, dict[str, tuple[int, int]]] = {"backend": {}, "ingest": {}}
    for prefix, sid in stack_of.items():
        cpus = sum(cpu_milli(v) for k, v in env.items() if k.startswith(f"{prefix}_") and k.endswith("_CPUS"))
        mem = sum(compose_mem(v) for k, v in env.items() if k.startswith(f"{prefix}_") and k.endswith("_MEM"))
        out["backend"][sid] = (cpus, mem)
        out["ingest"][sid] = (cpu_milli(env["INGEST_CPUS"]), compose_mem(env["INGEST_MEM"]))
    return out


_VAR = re.compile(r"\$\{(\w+)\}")


def compose_settings(docs: list[dict], env_path: Path) -> list[str]:
    """Problems: compose environment entries and profile-valued flags missing from the stack's container that runs
    the same image. A value naming a variable the profile lacks is a credential, which must come from a Secret."""
    profile = read_env(env_path)
    containers: dict[tuple[str, str], list[dict]] = {}
    for doc in docs:
        for meta, spec, _ in pod_templates(doc):
            sid = (meta.get("labels") or {}).get("otb.stack", "")
            for c in spec.get("containers", []):
                containers.setdefault((sid, split_image(normalise_image(c["image"]))[0]), []).append(c)
    problems = []
    for f in sorted((REPO / "stacks").glob("*/compose.yaml")):
        for name, svc in (yaml.safe_load(f.read_text()).get("services") or {}).items():
            repo = split_image(svc["image"])[0]
            found = containers.get((f.parent.name, repo), [])
            if not found:
                problems.append(f"{f.parent.name}/{name}: no rendered container runs {repo}")
                continue
            for key, raw in (svc.get("environment") or {}).items():
                if key in LOCAL_STORE_ONLY and all(set(c.get("args") or []) & OBJECT_STORES for c in found):
                    continue  # the gke profile runs Parseable on an object store, which has no local data dir
                value = _VAR.sub(lambda m: profile.get(m.group(1), m.group(0)), str(raw))
                credential = _VAR.search(value) is not None
                ok = any(
                    e["name"] == key and ("valueFrom" in e if credential else str(e.get("value")) == value)
                    for c in found
                    for e in c.get("env") or []
                ) or (
                    not credential
                    and any(  # a literal the chart reads from a Secret key it fixes
                        e["name"] == key and "valueFrom" in e for c in found for e in c.get("env") or []
                    )
                )
                if not ok:
                    problems.append(f"{f.parent.name}/{name}: env {key}={value} not set on the {repo} container")
            for arg in svc.get("command") or []:
                if _VAR.search(str(arg)) and str(arg).startswith("-"):
                    want = _VAR.sub(lambda m: profile[m.group(1)], str(arg))
                    if not any(want in (c.get("args") or []) + (c.get("command") or []) for c in found):
                        problems.append(f"{f.parent.name}/{name}: flag {want} missing from the {repo} container")
    return problems


def check(docs: list[dict], env: Path | None) -> tuple[dict[str, dict[str, tuple[int, int]]], list[str]]:
    lock = yaml.safe_load((REPO / "deploy/k8s/charts.lock.yaml").read_text())
    locked = {normalise_image(i) for i in lock["images"]}
    compose = {}
    for f in (REPO / "stacks").glob("*/compose.yaml"):
        for svc in (yaml.safe_load(f.read_text()).get("services") or {}).values():
            repo, digest = split_image(svc["image"])
            compose[repo] = digest

    problems: list[str] = []
    budgets: dict[str, dict[str, tuple[int, int]]] = {r: {} for r in ROLES}
    for doc in docs:
        meta = doc.get("metadata") or {}
        name = f"{doc.get('kind')}/{meta.get('name')}"
        if "test" in str((meta.get("annotations") or {}).get("helm.sh/hook", "")).split(","):
            problems.append(f"{name}: Helm test hook rendered")
        if doc.get("kind") == "PersistentVolumeClaim" and (doc.get("spec") or {}).get("storageClassName") == "":
            problems.append(f"{name}: storageClassName '' binds no provisioner")
        if doc.get("kind") == "StatefulSet":
            for vct in (doc.get("spec") or {}).get("volumeClaimTemplates") or []:
                if (vct.get("spec") or {}).get("storageClassName") == "":
                    problems.append(f"{name}: volumeClaimTemplate {vct['metadata']['name']}: storageClassName ''")
        for pod_meta, spec, replicas in pod_templates(doc):
            labels = pod_meta.get("labels") or {}
            sid, role = labels.get("otb.stack"), labels.get("otb.role")
            if not sid or role not in ROLES:
                problems.append(f"{name}: pod labels otb.stack/otb.role missing or invalid: {sid!r}/{role!r}")
            for c in spec.get("containers", []):
                if role in BUDGETED and "livenessProbe" in c:
                    problems.append(f"{name} container {c['name']}: livenessProbe on a {role} container")
            main = [container_budget(c, f"{name} container {c['name']}", problems) for c in spec.get("containers", [])]
            init = [
                container_budget(c, f"{name} init container {c['name']}", problems)
                for c in spec.get("initContainers") or []
            ]
            for c in [*spec.get("containers", []), *(spec.get("initContainers") or [])]:
                image = normalise_image(c["image"])
                repo, digest = split_image(image)
                if not digest or not digest.startswith("sha256:"):
                    problems.append(f"{name} container {c['name']}: image {c['image']!r} has no @sha256 digest")
                elif image not in locked and not any(split_image(i) == (repo, digest) for i in locked):
                    problems.append(f"{name} container {c['name']}: image {c['image']!r} is not in charts.lock.yaml")
                if repo in compose and compose[repo] != digest:
                    problems.append(f"{name} container {c['name']}: {repo} runs {digest}, compose runs {compose[repo]}")
            cpu = max(sum(c for c, _ in main), max((c for c, _ in init), default=0)) * replicas
            mem = max(sum(m for _, m in main), max((m for _, m in init), default=0)) * replicas
            if sid and role in ROLES:
                c0, m0 = budgets[role].get(sid, (0, 0))
                budgets[role][sid] = (c0 + cpu, m0 + mem)

    stack_ids = set(budgets["backend"]) | set(budgets["ingest"])
    for role in BUDGETED:
        per_stack = budgets[role]
        if len(set(per_stack.values())) > 1:
            detail = ", ".join(f"{k}={c}m/{m / 2**30:.2f}GiB" for k, (c, m) in sorted(per_stack.items()))
            problems.append(f"unequal {role} budgets: {detail}")
        for sid in sorted(stack_ids - set(per_stack)):
            problems.append(f"{sid}: no {role} pods")
    if env is not None:
        problems += compose_settings(docs, env)
        want = env_budgets(env)
        for role in BUDGETED:
            for sid, got in sorted(budgets[role].items()):
                if want[role].get(sid) != got:
                    problems.append(
                        f"{sid} {role}: rendered {got[0]}m/{got[1]} B, {env.name} says {want[role].get(sid)}"
                    )
    return budgets, problems


def render_stacks(profile: str) -> list[dict]:
    """Every release of every stack with a k8s block, rendered as the runtime renders it."""
    from otb.stacks.definition import registry
    from otb.stacks.k8s import render

    return [d for s in registry(REPO, "k8s").values() for r in s.k8s.releases for d in render(s, r, profile, REPO)]  # type: ignore[union-attr]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("rendered", nargs="?", type=Path, help="rendered YAML to check instead of rendering the stacks")
    p.add_argument("--profile", choices=["kind", "gke"], default="kind", help="budget profile to render")
    p.add_argument("--env", type=Path, help="compose profile whose budgets the render must equal")
    a = p.parse_args()
    if a.rendered:
        docs = [d for d in yaml.safe_load_all(a.rendered.read_text()) if isinstance(d, dict)]
    else:
        docs = render_stacks(a.profile)
    budgets, problems = check(docs, a.env)
    for role in ROLES:
        for sid, (cpu, mem) in sorted(budgets[role].items()):
            print(f"{sid:14s} {role:8s} {cpu / 1000:6.2f} CPU {mem / 2**30:7.2f} GiB")
    for prob in problems:
        print(f"FAIL {prob}")
    print(f"checked {len(docs)} objects: " + ("ok" if not problems else f"{len(problems)} problem(s)"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
