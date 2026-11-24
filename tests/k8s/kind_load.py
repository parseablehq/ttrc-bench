"""Load every image in deploy/k8s/charts.lock.yaml into a kind node under its index digest, so no pod pulls inside
the node.

    python tests/k8s/kind_load.py <kind node container> [arch]     e.g. otb-w1-control-plane arm64

`kind load docker-image` imports `docker save` output, whose manifests and layers docker rewrites, so a pod that
names the registry's index digest misses it and the kubelet pulls. Here the host fetches the index, the node's
platform manifest, its config and layers from the registry, checks every blob against its digest, imports them
with `ctr images import` under name:tag, and adds the name@digest reference the CRI resolves."""

import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
NODE = sys.argv[1]
ARCH = sys.argv[2] if len(sys.argv) > 2 else "arm64"
ACCEPT = ", ".join(
    [
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
    ]
)


def split(img: str) -> tuple[str, str, str, str]:
    """(registry host, repository path, tag, digest)."""
    name, digest = img.split("@")
    repo, tag = name.rsplit(":", 1)
    first = repo.split("/")[0]
    if "." in first:
        return first, repo.split("/", 1)[1], tag, digest
    return "registry-1.docker.io", repo if "/" in repo else f"library/{repo}", tag, digest


TOKENS: dict[tuple[str, str], str] = {}


def token(host: str, path: str) -> str | None:
    if (host, path) in TOKENS:
        return TOKENS[(host, path)]
    try:
        urllib.request.urlopen(f"https://{host}/v2/", timeout=30)
        return None
    except urllib.error.HTTPError as e:
        challenge = e.headers.get("WWW-Authenticate", "")
    params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
    url = f"{params['realm']}?service={params.get('service', '')}&scope=repository:{path}:pull"
    TOKENS[(host, path)] = json.loads(urllib.request.urlopen(url, timeout=30).read())["token"]
    return TOKENS[(host, path)]


def get(host: str, path: str, kind: str, ref: str) -> bytes:
    req = urllib.request.Request(f"https://{host}/v2/{path}/{kind}/{ref}", headers={"Accept": ACCEPT})
    t = token(host, path)
    if t:
        req.add_unredirected_header("Authorization", f"Bearer {t}")
    body = urllib.request.urlopen(req, timeout=600).read()
    if ref.startswith("sha256:") and "sha256:" + hashlib.sha256(body).hexdigest() != ref:
        sys.exit(f"{host}/{path} {kind} {ref}: content does not hash to its digest")
    return body


def full_name(host: str, path: str) -> str:
    return f"docker.io/{path}" if host == "registry-1.docker.io" else f"{host}/{path}"


images = re.findall(r"^  - (\S+@sha256:[0-9a-f]{64})", (REPO / "deploy/k8s/charts.lock.yaml").read_text(), re.M)
for img in images:
    host, path, tag, digest = split(img)
    index = get(host, path, "manifests", digest)
    doc = json.loads(index)
    plat = next(m for m in doc["manifests"] if m["platform"]["os"] == "linux" and m["platform"]["architecture"] == ARCH)
    manifest = get(host, path, "manifests", plat["digest"])
    mdoc = json.loads(manifest)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        blobs = root / "blobs" / "sha256"
        blobs.mkdir(parents=True)
        (blobs / digest[7:]).write_bytes(index)
        (blobs / plat["digest"][7:]).write_bytes(manifest)
        for b in [mdoc["config"], *mdoc["layers"]]:
            (blobs / b["digest"][7:]).write_bytes(get(host, path, "blobs", b["digest"]))
        (root / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')
        name = f"{full_name(host, path)}:{tag}"
        (root / "index.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 2,
                    "manifests": [
                        {
                            "mediaType": doc["mediaType"],
                            "digest": digest,
                            "size": len(index),
                            "annotations": {"io.containerd.image.name": name, "org.opencontainers.image.ref.name": tag},
                        }
                    ],
                }
            )
        )
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as t:
            for f in ("oci-layout", "index.json", "blobs"):
                t.add(root / f, arcname=f)
        subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                NODE,
                "ctr",
                "-n",
                "k8s.io",
                "images",
                "import",
                "--platform",
                f"linux/{ARCH}",
                "-",
            ],
            input=buf.getvalue(),
            check=True,
            capture_output=True,
        )
        ref = f"{full_name(host, path)}@{digest}"
        subprocess.run(
            ["docker", "exec", NODE, "ctr", "-n", "k8s.io", "images", "tag", "--force", name, ref],
            check=True,
            capture_output=True,
        )
        print(f"loaded {ref}", flush=True)
