"""deploy/k8s/charts.lock.yaml lists every image any TTRC-Bench pod runs on Kubernetes, and nothing else. Offline: the
plain manifests (background generators and their collector, the runner pod, the otb-fx fixture) are read as
committed; the stack releases and the demo are rendered when their chart archives are in .otb/charts and skipped
otherwise (tests/k8s/test_render_check.py and test_demo_k8s_render.py cover those renders in full).

Ways this could be wrong, each asserted below:
- a pod runs an image without a digest, or one the lock does not list (so a kind warm or a GKE pull of "every
  locked image" would miss it);
- the lock lists an image no manifest runs any more (a stale pin nobody checks);
- a kind warm of the lock would skip a registry the mirrors do not front.

Evidence: tests/e2e/artifacts/k8s_images_locked.json.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml
from conftest import REPO, write_artifact

LOCK = REPO / "deploy/k8s/charts.lock.yaml"
PINNED = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")
MIRRORED = ("docker.io", "ghcr.io", "quay.io", "gcr.io")
_POD_SPEC = {
    "Pod": ("spec",),
    "Deployment": ("spec", "template", "spec"),
    "StatefulSet": ("spec", "template", "spec"),
    "DaemonSet": ("spec", "template", "spec"),
    "Job": ("spec", "template", "spec"),
    "CronJob": ("spec", "jobTemplate", "spec", "template", "spec"),
}


def normalise(ref: str) -> str:
    """repository@digest: the docker.io prefixes and the tag dropped (a chart that takes a digest renders no tag)."""
    for prefix in ("docker.io/library/", "docker.io/"):
        if ref.startswith(prefix):
            ref = ref[len(prefix) :]
    name, _, digest = ref.partition("@")
    return re.sub(r":[^/:]+$", "", name) + ("@" + digest if digest else "")


def registry_of(ref: str) -> str:
    first = ref.split("/")[0]
    return first if "." in first else "docker.io"


def images_of(docs: list[dict]) -> set[str]:
    out = set()
    for d in docs:
        if d.get("kind") not in _POD_SPEC:
            continue
        node: object = d
        for key in _POD_SPEC[d["kind"]]:
            node = (node or {}).get(key)  # type: ignore[union-attr]
        spec = node or {}
        for c in spec.get("containers", []) + spec.get("initContainers", []):  # type: ignore[union-attr]
            out.add(normalise(str(c.get("image", ""))))
    return out


def plain_manifests() -> dict[str, list[dict]]:
    sources = {
        "background": sorted((REPO / "deploy/k8s/background").glob("*.yaml")),
        "runner": [REPO / "deploy/k8s/runner/statefulset.yaml", REPO / "deploy/k8s/runner/fixture-stack.yaml"],
        "stacks-manifests": sorted((REPO / "deploy/k8s/stacks").glob("*/*-kind.yaml"))
        + sorted((REPO / "deploy/k8s/stacks").glob("*/*-gke.yaml"))
        + sorted((REPO / "deploy/k8s/stacks").glob("*/*-token.yaml"))
        + sorted((REPO / "deploy/k8s/stacks").glob("*/objects.yaml")),
    }
    return {
        k: [d for p in v for d in yaml.safe_load_all(p.read_text()) if isinstance(d, dict)] for k, v in sources.items()
    }


def rendered() -> dict[str, list[dict]]:
    """The stack charts and the demo, when their archives are here."""
    from otb.stacks.definition import registry
    from otb.stacks.k8s import chart_archive, chart_releases, render

    out: dict[str, list[dict]] = {}
    stacks = list(registry(REPO, "k8s").values())
    if all(chart_archive(r, REPO).exists() for r in chart_releases(stacks)):
        out["stacks"] = [
            d for s in stacks for r in s.k8s.releases for p in ("kind", "gke") for d in render(s, r, p, REPO)
        ]  # type: ignore[union-attr]
    if (REPO / ".otb/charts/opentelemetry-demo-0.42.3.tgz").exists():
        p = subprocess.run(
            [str(REPO / "deploy/k8s/demo/install.sh"), "--template", "eu1", "kind"],
            capture_output=True,
            text=True,
            cwd=REPO,
        )
        assert p.returncode == 0, p.stderr
        out["demo"] = [d for d in yaml.safe_load_all(p.stdout) if isinstance(d, dict)]
    return out


def test_every_pod_image_is_in_the_lock_and_every_locked_image_runs(artifacts: Path) -> None:
    locked = [normalise(i) for i in yaml.safe_load(LOCK.read_text())["images"]]
    assert all(PINNED.match(i) for i in locked) and len(set(locked)) == len(locked)
    groups = plain_manifests() | rendered()
    used = {g: sorted(images_of(docs)) for g, docs in groups.items()}
    unpinned = {g: [i for i in v if not PINNED.match(i)] for g, v in used.items()}
    missing = {g: [i for i in v if i not in locked] for g, v in used.items()}
    every = {i for v in used.values() for i in v}
    unused = sorted(i for i in locked if i not in every)
    registries = sorted({registry_of(i) for i in locked})
    write_artifact(
        artifacts / "k8s_images_locked.json",
        {"groups": sorted(groups), "images": used, "unpinned": unpinned, "not_in_lock": missing, "unused": unused},
    )
    assert not any(unpinned.values()), unpinned
    assert not any(missing.values()), missing
    assert set(registries) <= set(MIRRORED), "deploy/k8s/kind/up.sh fronts every registry the lock names"
    if "stacks" in groups and "demo" in groups:
        assert unused == [], "every locked image is run by some pod"
    else:
        plain = {i for g in ("background", "runner", "stacks-manifests") for i in used[g]}
        assert plain <= set(locked)
