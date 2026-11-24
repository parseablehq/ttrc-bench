"""The Kubernetes runtime behind `otb up`, `down`, `bootstrap` and `doctor`: one helm release or kubectl apply
per `k8s.releases` entry, pinned charts in .otb/charts/, and the rendered-manifest checks that replace
`docker compose config` (Guaranteed pods, digests, equal per-role budgets). Readiness and provisioning stay in
lifecycle.py: they are HTTP under both runtimes.

Budgets: deploy/k8s/profiles/<profile>.yaml holds one values overlay per release name; the overlay is written to
.otb/k8s/values/<profile>/<release>.yaml and passed after the release's own values, so the profile wins. A
manifests release takes the profile through `{profile}` in its paths.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import yaml

from otb.stacks.definition import REPO, ChartRelease, ManifestRelease, Release, StackDef, StackDefError

ROLES = ("backend", "ingest", "setup")
PINNED_IMAGE = re.compile(r"^[^@\s]+:[^@\s]+@sha256:[0-9a-f]{64}$")  # name:tag@sha256:<digest>, both runtimes
_MEM = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "k": 10**3, "M": 10**6, "G": 10**9, "T": 10**12}
_POD_SPEC = {
    "Pod": ("spec",),
    "Deployment": ("spec", "template"),
    "StatefulSet": ("spec", "template"),
    "DaemonSet": ("spec", "template"),
    "ReplicaSet": ("spec", "template"),
    "Job": ("spec", "template"),
    "CronJob": ("spec", "jobTemplate", "spec", "template"),
}


@dataclass(frozen=True, slots=True)
class LockedChart:
    repo: str | None  # chart repository URL; None for an oci:// chart
    version: str
    sha256: str  # of the chart archive helm pull writes


def _run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
    """kubectl's and helm's own chatter stays off stdout, which `otb up` reserves for its report."""
    if "capture_output" not in kw and "stdout" not in kw:
        kw["stdout"] = subprocess.DEVNULL
    return subprocess.run(argv, text=True, check=True, **kw)  # type: ignore[call-overload]


def _kube(tool: str) -> list[str]:
    """kubectl and helm spell the context flag differently; both read the in-cluster service account when unset."""
    ctx = os.environ.get("OTB_K8S_CONTEXT")
    return [tool, "--context" if tool == "kubectl" else "--kube-context", ctx] if ctx else [tool]


POST_RENDERER = "no-liveness"  # the helm plugin in deploy/k8s/stacks/no-liveness/


def post_renderer() -> list[str]:
    """Every chart release renders through the no-liveness plugin: no liveness probe on a backend or ingest
    container, as compose never restarts a slow backend. helm 4 takes post-renderers as installed plugins."""
    return ["--post-renderer", POST_RENDERER]


def post_renderer_installed() -> bool:
    p = subprocess.run(["helm", "plugin", "list"], capture_output=True, text=True)
    return any(line.split()[:1] == [POST_RENDERER] for line in p.stdout.splitlines())


def install_post_renderer(repo: Path = REPO) -> bool:
    """`helm plugin install` from the repo directory, once; returns whether it installed now."""
    if post_renderer_installed():
        return False
    _run(["helm", "plugin", "install", str(repo / "deploy" / "k8s" / "stacks" / POST_RENDERER)])
    return True


def charts_dir(repo: Path = REPO) -> Path:
    return repo / ".otb" / "charts"


def chart_lock(repo: Path = REPO) -> dict[str, LockedChart]:
    path = repo / "deploy" / "k8s" / "charts.lock.yaml"
    if not path.exists():
        raise StackDefError(f"{path}: missing")
    raw = yaml.safe_load(path.read_text()) or {}
    charts = cast(dict[str, dict[str, object]], raw.get("charts") or {})
    out = {}
    for chart, entry in charts.items():
        try:
            out[chart] = LockedChart(cast(str | None, entry.get("repo")), str(entry["version"]), str(entry["sha256"]))
        except (KeyError, AttributeError) as e:
            raise StackDefError(f"{path}: charts.{chart}: needs version and sha256") from e
    return out


def chart_archive(release: ChartRelease, repo: Path = REPO) -> Path:
    return charts_dir(repo) / f"{release.chart.rsplit('/', 1)[-1]}-{release.version}.tgz"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def locked(release: ChartRelease, lock: dict[str, LockedChart]) -> LockedChart:
    entry = lock.get(release.chart)
    if entry is None:
        raise StackDefError(f"{release.name}: chart {release.chart} is not in deploy/k8s/charts.lock.yaml")
    if entry.version != release.version:
        raise StackDefError(
            f"{release.name}: chart {release.chart} version {release.version} differs from the lock's {entry.version}"
        )
    return entry


def pull_charts(stacks: list[StackDef], repo: Path = REPO) -> list[Path]:
    """`helm pull` every pinned chart the stacks release into .otb/charts/, once per (chart, version); an archive
    already there with the locked sha256 is kept, one with another sha256 is refused and removed."""
    lock = chart_lock(repo)
    charts_dir(repo).mkdir(parents=True, exist_ok=True)
    pulled: list[Path] = []
    for release in chart_releases(stacks):
        entry = locked(release, lock)
        archive = chart_archive(release, repo)
        if archive.exists() and _sha256(archive) == entry.sha256:
            continue
        archive.unlink(missing_ok=True)
        ref = (
            [release.chart]
            if release.chart.startswith("oci://")
            else [release.chart.rsplit("/", 1)[-1], "--repo", entry.repo or ""]
        )
        if not release.chart.startswith("oci://") and not entry.repo:
            raise StackDefError(f"{release.name}: chart {release.chart} has no repo in charts.lock.yaml")
        _run(["helm", "pull", *ref, "--version", release.version, "--destination", str(charts_dir(repo))])
        if not archive.exists():
            raise StackDefError(f"{release.name}: helm pull did not write {archive}")
        got = _sha256(archive)
        if got != entry.sha256:
            archive.unlink()
            raise StackDefError(f"{release.name}: {archive.name} sha256 {got} differs from the lock's {entry.sha256}")
        pulled.append(archive)
    return pulled


def chart_releases(stacks: list[StackDef]) -> list[ChartRelease]:
    seen: dict[tuple[str, str], ChartRelease] = {}
    for s in stacks:
        for r in _releases(s):
            if isinstance(r, ChartRelease):
                seen.setdefault((r.chart, r.version), r)
    return list(seen.values())


def _releases(stack: StackDef) -> tuple[Release, ...]:
    if stack.k8s is None:
        raise StackDefError(f"{stack.id}: stack.yaml has no k8s block; it cannot run under the k8s runtime")
    return stack.k8s.releases


def namespace(stack: StackDef) -> str:
    if stack.k8s is None:
        raise StackDefError(f"{stack.id}: stack.yaml has no k8s block; it cannot run under the k8s runtime")
    return stack.k8s.namespace


def profile_values(release: Release, profile: str, repo: Path = REPO) -> Path | None:
    """The release's overlay from deploy/k8s/profiles/<profile>.yaml, written where helm can read it."""
    src = repo / "deploy" / "k8s" / "profiles" / f"{profile}.yaml"
    if not src.exists():
        raise StackDefError(f"{src}: missing (k8s profile {profile})")
    overlays = cast(dict[str, object], yaml.safe_load(src.read_text()) or {})
    if release.name not in overlays or not isinstance(release, ChartRelease):
        return None
    out = repo / ".otb" / "k8s" / "values" / profile / f"{release.name}.yaml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump(overlays[release.name], sort_keys=True))
    return out


def _values_args(release: ChartRelease, profile: str, repo: Path) -> list[str]:
    args = ["-f", str(release.values)]
    overlay = profile_values(release, profile, repo)
    return args + (["-f", str(overlay)] if overlay else [])


ROLLOUT_TIMEOUT = "15m"
JOB_TIMEOUT_S = 1500.0
WAITABLE = ("Deployment", "StatefulSet", "DaemonSet")


def release_argv(stack: StackDef, release: Release, profile: str, repo: Path = REPO) -> list[str]:
    ns = namespace(stack)
    if isinstance(release, ManifestRelease):
        files = [a for p in release.paths(profile, repo) for a in ("-f", str(p))]
        return [*_kube("kubectl"), "apply", "--namespace", ns, *files]
    archive = chart_archive(release, repo)
    if not archive.exists():
        raise StackDefError(f"{stack.id}: {archive} missing; run `otb bootstrap --runtime k8s` first")
    return [
        *_kube("helm"),
        "upgrade",
        "--install",
        release.name,
        str(archive),
        "--namespace",
        ns,
        "--create-namespace",
        *_values_args(release, profile, repo),
        *post_renderer(),
        "--wait",
        "--wait-for-jobs",
        "--timeout",
        ROLLOUT_TIMEOUT,
    ]


def credentials_manifest(stack: StackDef, values: dict[str, str]) -> str:
    """The stack's namespace and its credential Secret `otb-credentials` (keys: the credential names), applied
    before the releases so chart values can reference them."""
    ns = namespace(stack)
    docs = [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns, "labels": {"otb.stack": stack.id}}},
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "otb-credentials", "namespace": ns, "labels": {"otb.stack": stack.id}},
            "type": "Opaque",
            "stringData": {c.name: values[c.name] for c in stack.credentials if c.name in values},
        },
    ]
    return yaml.safe_dump_all(docs, sort_keys=True)


def _get_json(argv: list[str]) -> dict | list | None:
    """kubectl/helm `-o json` output, or None when the object or release does not exist."""
    p = subprocess.run(argv, capture_output=True, text=True)
    if p.returncode != 0 or not p.stdout.strip():
        return None
    return cast(dict | list, json.loads(p.stdout))


def unstick(ns: str, release: str) -> str | None:
    """An interrupted `up` leaves a helm release pending, and helm then refuses every upgrade. Roll back to the
    last revision that deployed, or uninstall a release that never did. Returns the status it cleared."""
    history = cast(list[dict] | None, _get_json([*_kube("helm"), "history", release, "--namespace", ns, "-o", "json"]))
    if not history or not str(history[-1]["status"]).startswith("pending"):
        return None
    deployed = [h["revision"] for h in history if h["status"] in ("deployed", "superseded")]
    if deployed:
        _run([*_kube("helm"), "rollback", release, str(deployed[-1]), "--namespace", ns, "--wait"])
    else:
        _run([*_kube("helm"), "uninstall", release, "--namespace", ns, "--wait"])
    return str(history[-1]["status"])


def recreate_strategy(ns: str, docs: list[dict]) -> list[str]:
    """A live Deployment on RollingUpdate refuses a template that switches it to Recreate (`rollingUpdate` is
    forbidden with Recreate, and the server-side merge keeps the old block). Patch the live strategy first.
    Returns the Deployments patched."""
    patched = []
    for d in docs:
        if d.get("kind") != "Deployment" or ((d.get("spec") or {}).get("strategy") or {}).get("type") != "Recreate":
            continue
        name = d["metadata"]["name"]
        live = cast(
            dict | None, _get_json([*_kube("kubectl"), "get", "deployment", name, "--namespace", ns, "-o", "json"])
        )
        if live is None or ((live.get("spec") or {}).get("strategy") or {}).get("type") == "Recreate":
            continue
        patch = json.dumps({"spec": {"strategy": {"type": "Recreate", "rollingUpdate": None}}})
        _run([*_kube("kubectl"), "patch", "deployment", name, "--namespace", ns, "--type", "merge", "-p", patch])
        patched.append(name)
    return patched


def _jobs(docs: list[dict]) -> list[str]:
    return [d["metadata"]["name"] for d in docs if d.get("kind") == "Job"]


def wait_job(ns: str, name: str, timeout_s: float = JOB_TIMEOUT_S) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        job = cast(dict | None, _get_json([*_kube("kubectl"), "get", "job", name, "--namespace", ns, "-o", "json"]))
        conditions = ((job or {}).get("status") or {}).get("conditions") or []
        done = {c["type"] for c in conditions if c.get("status") == "True"}
        if "Complete" in done:
            return
        if "Failed" in done:
            raise StackDefError(f"{ns}/job/{name} failed: see `kubectl -n {ns} logs job/{name}`")
        if time.monotonic() >= deadline:
            raise StackDefError(f"{ns}/job/{name} not complete after {timeout_s:.0f}s")
        time.sleep(2)


def wait_manifests(ns: str, docs: list[dict]) -> None:
    """Every workload rolled out and every Job complete: the equivalent of helm's --wait --wait-for-jobs."""
    for d in docs:
        kind, name = d.get("kind"), (d.get("metadata") or {}).get("name")
        if kind in WAITABLE:
            ref = f"{kind.lower()}/{name}"
            _run([*_kube("kubectl"), "rollout", "status", ref, "--namespace", ns, "--timeout", ROLLOUT_TIMEOUT])
        elif kind == "Job":
            wait_job(ns, str(name))


def converge_release(stack: StackDef, release: Release, profile: str, repo: Path = REPO) -> dict[str, object]:
    """One release to its rendered state, whatever an earlier run left: a pending helm release is reset, a
    Deployment's strategy change is made applicable, the release's Jobs run again (both setup scripts change
    nothing when their Secret is already right), and the call returns only when every workload is rolled out and
    every Job complete."""
    ns = namespace(stack)
    docs = render(stack, release, profile, repo)
    note: dict[str, object] = {}
    if isinstance(release, ManifestRelease):
        for job in _jobs(docs):
            _run([*_kube("kubectl"), "delete", "job", job, "--namespace", ns, "--ignore-not-found", "--wait"])
        _run(release_argv(stack, release, profile, repo))
        wait_manifests(ns, docs)
        note["jobs"] = _jobs(docs)
    else:
        if was := unstick(ns, release.name):
            note["unstuck_from"] = was
        if patched := recreate_strategy(ns, docs):
            note["strategy_patched"] = patched
        _run(release_argv(stack, release, profile, repo))
    return note


def apply(
    stacks: list[StackDef], profile: str, secrets: dict[str, dict[str, str]], repo: Path = REPO
) -> dict[str, dict[str, dict[str, object]]]:
    """Namespace and Secret, then every release in order, per stack. Each step converges: a second apply changes
    nothing, and one interrupted anywhere resumes."""
    applied: dict[str, dict[str, dict[str, object]]] = {}
    for s in stacks:
        _run([*_kube("kubectl"), "apply", "-f", "-"], input=credentials_manifest(s, secrets.get(s.id, {})))
        applied[s.id] = {r.name: converge_release(s, r, profile, repo) for r in _releases(s)}
    return applied


def remove(stacks: list[StackDef], profile: str, volumes: bool, repo: Path = REPO) -> None:
    for s in stacks:
        ns = namespace(s)
        for r in reversed(_releases(s)):
            if isinstance(r, ManifestRelease):
                files = [a for p in r.paths(profile, repo) for a in ("-f", str(p))]
                _run([*_kube("kubectl"), "delete", "--namespace", ns, "--ignore-not-found", "--wait", *files])
            else:
                _run([*_kube("helm"), "uninstall", r.name, "--namespace", ns, "--ignore-not-found", "--wait"])
        if volumes:
            _run([*_kube("kubectl"), "delete", "pvc", "--all", "--namespace", ns, "--wait"])


# ---------- doctor: rendered manifests ----------


def render(stack: StackDef, release: Release, profile: str, repo: Path = REPO) -> list[dict]:
    if isinstance(release, ManifestRelease):
        text = "\n---\n".join(p.read_text() for p in release.paths(profile, repo))
    else:
        argv = [*_kube("helm"), "template", release.name, str(chart_archive(release, repo))]
        argv += ["--namespace", namespace(stack), *_values_args(release, profile, repo), *post_renderer()]
        text = _run(argv, capture_output=True).stdout
    return [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]


def cpu_cores(q: object) -> float:
    s = str(q)
    return int(s[:-1]) / 1000 if s.endswith("m") else float(s)


def mem_bytes(q: object) -> int:
    s = str(q)
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(Ki|Mi|Gi|Ti|k|M|G|T)?", s)
    if not m:
        raise ValueError(f"unparseable memory quantity {q!r}")
    return int(float(m.group(1)) * _MEM.get(m.group(2) or "", 1))


@dataclass(frozen=True, slots=True)
class PodTemplate:
    where: str  # "<release>: <Kind>/<name>"
    replicas: int
    labels: dict[str, str]
    containers: list[dict]
    init_containers: list[dict]


def pod_templates(release: str, docs: list[dict]) -> Iterator[PodTemplate]:
    """Every pod template in the rendered docs, named "<release>: <Kind>/<name>"."""
    for d in docs:
        kind = d.get("kind")
        if kind not in _POD_SPEC:
            continue
        node: object = d
        for key in _POD_SPEC[kind]:
            node = cast(dict, node).get(key) or {}
        template = cast(dict, node)
        spec = template.get("spec") or {}
        where = f"{release}: {kind}/{(d.get('metadata') or {}).get('name')}"
        replicas = (
            int((d.get("spec") or {}).get("replicas", 1) or 0)
            if kind in ("Deployment", "StatefulSet", "ReplicaSet")
            else 1
        )
        labels = dict((template.get("metadata") or {}).get("labels") or {})
        yield PodTemplate(
            where, replicas, labels, list(spec.get("containers") or []), list(spec.get("initContainers") or [])
        )


def qos_guaranteed(c: dict) -> tuple[float, int] | None:
    """(cpu, memory) when requests equal limits for both, else None."""
    res = c.get("resources") or {}
    req, lim = res.get("requests") or {}, res.get("limits") or {}
    try:
        if not all(k in req and k in lim for k in ("cpu", "memory")):
            return None
        cpu, mem = cpu_cores(lim["cpu"]), mem_bytes(lim["memory"])
        if cpu_cores(req["cpu"]) != cpu or mem_bytes(req["memory"]) != mem:
            return None
    except ValueError:
        return None
    return cpu, mem


def budgets(
    stacks: list[StackDef], profile: str, repo: Path = REPO
) -> tuple[dict[str, dict[str, tuple[float, int]]], list[str]]:
    """Per (role, stack) CPU and memory sums over the rendered pods (main containers times replicas), and the
    problems found on the way: unlabelled pods, non-Guaranteed containers, images without digests."""
    problems: list[str] = []
    sums: dict[str, dict[str, tuple[float, int]]] = {}
    stack_ids = {s.id for s in stacks}
    for s in stacks:
        for r in _releases(s):
            for pod in pod_templates(r.name, render(s, r, profile, repo)):
                prefix = f"{profile}: {s.id}/{pod.where}"
                for c in pod.containers + pod.init_containers:
                    if not PINNED_IMAGE.match(str(c.get("image", ""))):
                        problems.append(
                            f"{prefix}: container {c.get('name')}: image {c.get('image')!r} is not pinned by digest"
                        )
                sid, role = pod.labels.get("otb.stack"), pod.labels.get("otb.role")
                if sid not in stack_ids or role not in ROLES:
                    problems.append(f"{prefix}: labels otb.stack/otb.role missing or invalid")
                    continue
                shares = [qos_guaranteed(c) for c in pod.containers + pod.init_containers]
                if any(g is None for g in shares):
                    bad = [
                        c.get("name")
                        for c, g in zip(pod.containers + pod.init_containers, shares, strict=True)
                        if g is None
                    ]
                    problems.append(f"{prefix}: not Guaranteed (requests must equal limits for cpu and memory): {bad}")
                    continue
                if role == "setup":
                    continue
                cpu = sum(g[0] for g in shares[: len(pod.containers)]) * pod.replicas
                mem = sum(g[1] for g in shares[: len(pod.containers)]) * pod.replicas
                c0, m0 = sums.setdefault(role, {}).get(sid, (0.0, 0))
                sums[role][sid] = (round(c0 + cpu, 3), m0 + mem)
    return sums, problems


def doctor(stacks: list[StackDef], profiles: list[str], repo: Path = REPO) -> list[str]:
    problems = [
        f"{s.id}: stack.yaml has no k8s block; it cannot run under the k8s runtime" for s in stacks if s.k8s is None
    ]
    if problems:
        return problems
    try:
        lock = chart_lock(repo)
    except StackDefError as e:
        return [str(e)]
    for release in chart_releases(stacks):
        try:
            entry = locked(release, lock)
        except StackDefError as e:
            problems.append(str(e))
            continue
        archive = chart_archive(release, repo)
        if not archive.exists() or _sha256(archive) != entry.sha256:
            problems.append(
                f"{release.name}: {archive} missing or differs from charts.lock.yaml; run `otb bootstrap --runtime k8s`"
            )
    if problems:
        return problems
    if not post_renderer_installed():
        return [f"helm plugin {POST_RENDERER} is not installed; run `otb bootstrap --runtime k8s`"]
    from otb.background import pod_problems

    problems += pod_problems(repo)  # the background's pods: Guaranteed and pinned, outside the per-stack sums
    stack_ids = {s.id for s in stacks}
    for profile in profiles:
        sums, found = budgets(stacks, profile, repo)
        problems += found
        for role, per_stack in sorted(sums.items()):
            if len(set(per_stack.values())) > 1:
                detail = ", ".join(f"{k}={c} CPU/{m / 2**30:.2f} GiB" for k, (c, m) in sorted(per_stack.items()))
                problems.append(f"{profile}: unequal {role} budgets: {detail}")
            missing = stack_ids - set(per_stack)
            if missing:
                problems.append(f"{profile}: no {role} pods for {sorted(missing)}")
    return problems


def budgets_table(stacks: list[StackDef], profile: str, repo: Path = REPO) -> dict[str, dict[str, object]]:
    sums, _ = budgets(stacks, profile, repo)
    return {
        f"{sid}/{role}": {"cpus": cpu, "mem_bytes": mem}
        for role, per_stack in sums.items()
        if role != "setup"
        for sid, (cpu, mem) in per_stack.items()
    }
