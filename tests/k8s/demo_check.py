"""Checks a rendered OpenTelemetry Demo instance (deploy/k8s/demo/install.sh --template) against what TTRC-Bench needs
and against the Compose capture it reproduces. Prints one problem per line and exits 1 when there is any.

    python tests/k8s/demo_check.py <rendered.yaml> --instance <i> [--profile gke|kind]
    python tests/k8s/demo_check.py --snapshot <docker compose config --format json> > capture/demo-compose-gke.json

The ways a rendered instance can be wrong, each a check below:
- a container, init container or sidecar without cpu and memory requests equal to its limits, so the pod is not
  Guaranteed;
- an image that is not pinned by digest, or whose digest is not the Compose capture's (capture/demo-source-local.yaml);
- a Deployment for a service the Compose capture does not run (Kafka, the agent, OpAMP), or one missing;
- a service whose environment or ports differ from the Compose capture's, beyond the exceptions named here;
- on the gke profile, a memory limit that differs from the Compose capture's (so memory faults hit another limit),
  beyond the exceptions named here;
- a collector pipeline that exports anywhere but the TTRC-Bench gateway, or skips the instance labels or the batching;
- the gateway exporter's queue not on the persistent volume, or a rollout that would need the volume twice;
- kubeletstats without the namespace filter, or docker_stats still configured;
- a cluster-scoped object whose name does not carry the instance, so two instances collide.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "capture" / "demo-compose-gke.json"
SOURCE = REPO / "capture" / "demo-source-local.yaml"
DIGEST = re.compile(r"@(sha256:[0-9a-f]{64})$")
# flagd-ui is a sidecar of the flagd pod in the chart, not a service of its own.
ENV_EXCEPTIONS = {("frontend-proxy", "FLAGD_UI_HOST"): "flagd"}
# At Compose's 20Mi these Go services spin their CPU limit and time out requests; no memory fault targets them.
MEMORY_EXCEPTIONS = {"checkout": 64 << 20, "product-catalog": 64 << 20}
GATEWAY = "otlp_http/otb"
QUEUE_DIR = "/var/lib/otelcol/otb-queue"
UNITS = {"Ki": 1 << 10, "Mi": 1 << 20, "Gi": 1 << 30, "k": 10**3, "M": 10**6, "G": 10**9}


def quantity(q: object) -> float:
    s = str(q)
    if s.endswith("m"):
        return float(s[:-1]) / 1000
    for suffix, mult in UNITS.items():
        if s.endswith(suffix):
            return float(s[: -len(suffix)]) * mult
    return float(s)


def snapshot(compose_config: dict) -> dict:
    """The parts of `docker compose config` a demo instance must reproduce, without host paths."""
    out = {}
    for name, s in sorted(compose_config["services"].items()):
        limits = (s.get("deploy") or {}).get("resources", {}).get("limits", {})
        out[name] = {
            "image": s["image"],
            "environment": {k: v for k, v in sorted((s.get("environment") or {}).items()) if v is not None},
            "ports": sorted({p["target"] for p in s.get("ports", [])}),
            "memory_bytes": int(limits["memory"]) if "memory" in limits else None,
        }
    return out


def containers(pod: dict) -> list[tuple[str, dict]]:
    return [("init", c) for c in pod.get("initContainers") or []] + [("", c) for c in pod["containers"]]


def check(docs: list[dict], instance: str, profile: str, compose: dict, source: dict) -> list[str]:
    problems: list[str] = []
    deployments = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Deployment"}
    services = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Service"}

    for name, dep in sorted(deployments.items()):
        pod = dep["spec"]["template"]["spec"]
        for kind, c in containers(pod):
            where = f"{name}/{kind + ':' if kind else ''}{c['name']}"
            res = c.get("resources") or {}
            for r in ("cpu", "memory"):
                req, lim = (res.get("requests") or {}).get(r), (res.get("limits") or {}).get(r)
                if req is None or lim is None or quantity(req) != quantity(lim):
                    problems.append(f"{where}: {r} request {req} and limit {lim} are not equal, so not Guaranteed")
            if not DIGEST.search(c["image"]):
                problems.append(f"{where}: image {c['image']} is not pinned by digest")

    want = set(compose) - {"flagd-ui"}
    for extra in sorted(set(deployments) - want):
        problems.append(f"Deployment {extra}: the Compose capture runs no such service")
    for missing in sorted(want - set(deployments)):
        problems.append(f"Deployment {missing}: missing; the Compose capture runs it")

    by_container = {
        c["name"]: (dname, c)
        for dname, d in deployments.items()
        for kind, c in containers(d["spec"]["template"]["spec"])
        if not kind
    }
    for svc, spec in sorted(compose.items()):
        if svc not in by_container:
            continue
        dname, c = by_container[svc]
        m = DIGEST.search(c["image"])
        pinned = source["demo_collector_image"] if svc == "otel-collector" else source["service_images"].get(svc)
        if m and pinned and not pinned.endswith("@" + m.group(1)):
            problems.append(f"{svc}: digest {m.group(1)} is not the Compose capture's {pinned}")
        if profile == "gke" and spec["memory_bytes"] is not None:
            have = quantity(c["resources"]["limits"]["memory"])
            want = MEMORY_EXCEPTIONS.get(svc, spec["memory_bytes"])
            if have != want:
                problems.append(f"{svc}: memory limit {have:.0f} B, want {want} (Compose's {spec['memory_bytes']})")
        if svc == "otel-collector":
            continue
        env = {e["name"]: e.get("value") for e in c.get("env") or []}
        want_env = {k: ENV_EXCEPTIONS.get((svc, k), v) for k, v in spec["environment"].items()}
        for k in sorted(set(env) | set(want_env)):
            if env.get(k) != want_env.get(k):
                problems.append(f"{svc}: env {k} is {env.get(k)!r}, the Compose capture's is {want_env.get(k)!r}")
        ports = {p["port"] for p in (services.get(dname) or {}).get("spec", {}).get("ports", [])}
        for p in spec["ports"]:
            if p not in ports:
                problems.append(f"{svc}: Service {dname} lacks the Compose capture's port {p}")

    problems += check_collector(docs, deployments.get("otel-collector"))

    for d in docs:
        cluster_scoped = d["kind"] in ("ClusterRole", "ClusterRoleBinding", "Namespace", "PersistentVolume")
        if cluster_scoped and f"otb-demo-{instance}" not in d["metadata"]["name"]:
            problems.append(f"{d['kind']} {d['metadata']['name']}: cluster-scoped and not named for the instance")
    return problems


def check_collector(docs: list[dict], dep: dict | None) -> list[str]:
    if dep is None:
        return ["Deployment otel-collector: missing"]
    problems = []
    relay = next(d for d in docs if d["kind"] == "ConfigMap" and "relay" in (d.get("data") or {}))
    cfg = yaml.safe_load(relay["data"]["relay"])
    if "docker_stats" in cfg["receivers"]:
        problems.append("collector: docker_stats is configured; on Kubernetes container metrics come from kubeletstats")
    for name, p in sorted(cfg["service"]["pipelines"].items()):
        if name.startswith("profiles"):
            continue
        if GATEWAY not in p["exporters"] or set(p["exporters"]) - {GATEWAY, "span_metrics"}:
            problems.append(f"collector pipeline {name}: exports to {p['exporters']}, not only the TTRC-Bench gateway")
        if p["processors"][-2:] != ["resource/otb_instance", "batch/otb"]:
            problems.append(f"collector pipeline {name}: does not end with resource/otb_instance, batch/otb")
        if "kubeletstats" in p["receivers"] and p["processors"][0] != "filter/otb_namespace":
            problems.append(f"collector pipeline {name}: kubeletstats is not filtered to the instance's namespace")
    if cfg["exporters"][GATEWAY]["sending_queue"].get("storage") != "file_storage/otb":
        problems.append("collector: the gateway exporter's queue is not on file_storage/otb")
    if cfg["extensions"].get("file_storage/otb", {}).get("directory") != QUEUE_DIR:
        problems.append(f"collector: file_storage/otb is not at {QUEUE_DIR}")
    pod = dep["spec"]["template"]["spec"]
    vols = {v["name"]: v for v in pod.get("volumes") or []}
    mounts = {m["mountPath"]: m["name"] for m in pod["containers"][0].get("volumeMounts") or []}
    claim = (vols.get(mounts.get(QUEUE_DIR, "")) or {}).get("persistentVolumeClaim", {}).get("claimName")
    if claim is None or not any(d["kind"] == "PersistentVolumeClaim" and d["metadata"]["name"] == claim for d in docs):
        problems.append(f"collector: {QUEUE_DIR} is not a PersistentVolumeClaim of this release")
    if dep["spec"].get("strategy", {}).get("type") != "Recreate":
        problems.append("collector: rollout is not Recreate, so a new pod waits on the old pod's ReadWriteOnce volume")
    return problems


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="demo_check")
    p.add_argument("rendered", type=Path, nargs="?")
    p.add_argument("--instance")
    p.add_argument("--profile", default="gke")
    p.add_argument("--compose", type=Path, default=COMPOSE)
    p.add_argument("--source", type=Path, default=SOURCE)
    p.add_argument("--snapshot", type=Path, help="print the snapshot of this `docker compose config` JSON")
    a = p.parse_args(argv)
    if a.snapshot:
        print(json.dumps(snapshot(json.loads(a.snapshot.read_text())), indent=1))
        return 0
    if a.rendered is None or a.instance is None:
        p.error("rendered manifests and --instance are required")
    docs = [d for d in yaml.safe_load_all(a.rendered.read_text()) if d]
    compose, source = json.loads(a.compose.read_text()), yaml.safe_load(a.source.read_text())
    problems = check(docs, a.instance, a.profile, compose, source)
    for line in problems:
        print(line)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
