#!/usr/bin/env python3
"""Helm post-renderer for the stack releases: removes every livenessProbe from the containers of pods labelled
otb.role backend or ingest. Compose restarts a container only when it exits; a liveness probe also restarts
one that is slow to answer, which the gate's queries made Prometheus on kind. Readiness probes stay (they
only hold a pod out of its Service). The k8s runtime passes this to `helm upgrade` and `helm template`, so
doctor and the render checker see what the cluster gets; tests/k8s/check_rendered.py refuses a probe that
slipped through."""

from __future__ import annotations

import sys

import yaml

ROLES = ("backend", "ingest")
POD_SPEC = {
    "Pod": ("spec",),
    "Deployment": ("spec", "template"),
    "StatefulSet": ("spec", "template"),
    "DaemonSet": ("spec", "template"),
}


def strip(doc: dict) -> int:
    keys = POD_SPEC.get(doc.get("kind", ""))
    if not keys:
        return 0
    node = doc
    for k in keys:
        node = node.get(k) or {}
    labels = (node.get("metadata") or {}).get("labels") or {}
    if labels.get("otb.role") not in ROLES:
        return 0
    spec = node.get("spec") or {}
    return sum(1 for c in spec.get("containers") or [] if c.pop("livenessProbe", None) is not None)


def main() -> int:
    docs = list(yaml.safe_load_all(sys.stdin.read()))
    for d in docs:
        if isinstance(d, dict):
            strip(d)
    sys.stdout.write(yaml.safe_dump_all([d for d in docs if d is not None], sort_keys=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
