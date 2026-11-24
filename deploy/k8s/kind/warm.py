"""Fetch every blob a linux node needs for each pinned image through the kind pull-through caches (up.sh), so a
new kind cluster pulls at disk speed. A host `docker pull` would skip layers the host already has and leave the
cache cold. The list defaults to every image in deploy/k8s/charts.lock.yaml, which is every image any TTRC-Bench pod
runs.

    uv run python deploy/k8s/kind/warm.py [--arch arm64|amd64] [image@sha256:... ...]
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import urllib.request
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
MIRRORS = {"ghcr.io": 5101, "docker.io": 5102, "quay.io": 5103, "gcr.io": 5104}
ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)
ARCH = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64", "amd64": "amd64"}


def split(ref: str) -> tuple[str, str, str]:
    """(registry, repository, digest) of name[:tag]@sha256:..."""
    name, digest = ref.strip().split("@")
    name = name.rsplit(":", 1)[0] if ":" in name.split("/")[-1] else name
    first = name.split("/")[0]
    if "." in first:
        registry, repo = first, name[len(first) + 1 :]
    else:
        registry, repo = "docker.io", name if "/" in name else f"library/{name}"
    return registry, repo, digest


def get(url: str, accept: str | None = None) -> urllib.request.addinfourl:
    req = urllib.request.Request(url, headers={"Accept": accept} if accept else {})
    return urllib.request.urlopen(req, timeout=600)


def drain(url: str) -> int:
    n = 0
    with get(url) as r:
        while chunk := r.read(1 << 20):
            n += len(chunk)
    return n


def warm(arch: str, ref: str) -> int:
    registry, repo, digest = split(ref)
    base = f"http://127.0.0.1:{MIRRORS[registry]}/v2/{repo}"
    with get(f"{base}/manifests/{digest}", ACCEPT) as r:
        if r.headers["Docker-Content-Digest"] != digest:
            raise SystemExit(f"{ref}: cache served {r.headers['Docker-Content-Digest']}")
        doc = json.load(r)
    if "manifests" in doc:
        (m,) = [
            m
            for m in doc["manifests"]
            if m.get("platform", {}).get("architecture") == arch and m["platform"].get("os") == "linux"
        ]
        with get(f"{base}/manifests/{m['digest']}", ACCEPT) as r:
            doc = json.load(r)
    return sum(drain(f"{base}/blobs/{b['digest']}") for b in [doc["config"], *doc["layers"]])


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--arch", default=ARCH[platform.machine().lower()], choices=["arm64", "amd64"])
    p.add_argument("images", nargs="*", help="default: the images in deploy/k8s/charts.lock.yaml")
    a = p.parse_args()
    images = a.images or yaml.safe_load((REPO / "deploy/k8s/charts.lock.yaml").read_text())["images"]
    total = 0
    for ref in images:
        n = warm(a.arch, ref)
        total += n
        print(f"{n:>12} {ref}", flush=True)
    print(f"{total:>12} total", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
