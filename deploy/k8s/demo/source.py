"""Writes the `otb capture --source` file for demo instances on Kubernetes: the image each running container was
started from, read from its pod status (the digest the node resolved), as capture/demo-source-local.yaml records the
Compose capture's from `docker compose images`.

    uv run python deploy/k8s/demo/source.py <instance>... > capture/demo-source-k8s.yaml

Kube context from $OTB_K8S_CONTEXT (unset: kubectl's own, or in-cluster). Exits 1, writing nothing, when a running
container's image is not the digest its Deployment pins, when instances run different images, or when a pod of a
Deployment is not running.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
CHART = "open-telemetry/opentelemetry-demo 0.42.3"
DIGEST = re.compile(r"@(sha256:[0-9a-f]{64})$")


def kubectl(*args: str) -> dict:
    ctx = os.environ.get("OTB_K8S_CONTEXT")
    argv = ["kubectl", *(["--context", ctx] if ctx else []), *args, "-o", "json"]
    return json.loads(subprocess.run(argv, check=True, capture_output=True, text=True).stdout)


def repository(ref: str) -> str:
    """ghcr.io/open-telemetry/demo:latest-cart@sha256:... -> ghcr.io/open-telemetry/demo"""
    head, _, last = ref.split("@")[0].rpartition("/")
    return f"{head}/{last.split(':')[0]}" if head else last.split(":")[0]


def running_images(namespace: str) -> tuple[dict[str, str], list[str]]:
    """{container name: repository@digest} of every container of the namespace's running pods, and the problems."""
    images: dict[str, str] = {}
    problems = []
    for pod in kubectl("get", "pods", "-n", namespace)["items"]:
        name = pod["metadata"]["name"]
        if pod["status"].get("phase") != "Running" or pod["metadata"].get("deletionTimestamp"):
            continue
        spec = {c["name"]: c["image"] for c in pod["spec"]["containers"]}
        for st in pod["status"].get("containerStatuses", []):
            pinned = DIGEST.search(spec[st["name"]])
            ran = DIGEST.search(st.get("imageID", ""))
            if not pinned or not ran or pinned.group(1) != ran.group(1):
                problems.append(f"{namespace}/{name}/{st['name']}: runs {st.get('imageID')}, pins {spec[st['name']]}")
                continue
            images[st["name"]] = f"{repository(spec[st['name']])}@{ran.group(1)}"
    deployments = {d["metadata"]["name"] for d in kubectl("get", "deployments", "-n", namespace)["items"]}
    problems += [f"{namespace}: Deployment {d} has no running pod" for d in sorted(deployments - set(images))]
    return images, problems


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    catalogue = yaml.safe_load((REPO / "scenarios" / "faults.yaml").read_text())
    seen: dict[str, dict[str, str]] = {}
    problems = []
    for inst in argv:
        images, p = running_images(f"otb-demo-{inst}")
        seen[inst] = images
        problems += p
    first = next(iter(seen.values()))
    problems += [f"{i}: runs other images than {argv[0]}" for i, imgs in seen.items() if imgs != first]
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    collector = first.pop("opentelemetry-collector")
    out = {
        "demo_repo": catalogue["demo_repo"],
        "demo_commit": catalogue["demo_commit"],
        "demo_tag": catalogue["demo_tag"],
        "chart": CHART,
        "instances": list(argv),
        "demo_collector_image": collector,
        "service_images": dict(sorted(first.items())),
    }
    sys.stdout.write(yaml.safe_dump(out, sort_keys=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
