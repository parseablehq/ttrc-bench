"""The background volume (deploy/k8s/background/) as the k8s capture path runs it: the committed manifests with
the two rate ConfigMaps solved for a scenario's `background` (rate.yaml states the equations), applied before
the capture starts and removed after it ends, and the pod checks `otb doctor --runtime k8s` makes on them.

The generators' OTLP/JSON bytes at the gateway are records per second times the measured bytes per record
(`otb.bg/json-bytes-per-record` on each ConfigMap):
  logs     LOGS_PER_MIN / 60 * bytes_per_record
  metrics  WORKERS * DATAPOINTS_PER_REQ * 1000 / INTERVAL_MS * bytes_per_record
LOGS_PER_MIN and INTERVAL_MS are the knobs solved here; WORKERS, DATAPOINTS_PER_REQ, PODS and the field profile
(cardinality and shape) stay as committed."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

from otb.scenario import Background, Scenario
from otb.stacks.definition import REPO

NAMESPACE = "otb-bg"
ROLLOUT_TIMEOUT = "15m"


def _kubectl(context: str | None, *args: str) -> list[str]:
    ctx = context or os.environ.get("OTB_K8S_CONTEXT")
    return ["kubectl", *(["--context", ctx] if ctx else []), *args]


def read_manifests(repo: Path = REPO) -> list[dict]:
    """deploy/k8s/background/*.yaml as committed, in file order."""
    return [
        d
        for p in sorted((repo / "deploy" / "k8s" / "background").glob("*.yaml"))
        for d in yaml.safe_load_all(p.read_text())
        if isinstance(d, dict)
    ]


def _configmap(docs: list[dict], name: str) -> dict:
    return next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == name)


def bytes_per_record(cm: dict) -> float:
    return float(cm["metadata"]["annotations"]["otb.bg/json-bytes-per-record"])


def rates(docs: list[dict]) -> dict[str, float]:
    """The OTLP/JSON MB/s the rate ConfigMaps imply, per signal."""
    lg, ld = _configmap(docs, "otb-bg-loggen"), _configmap(docs, "otb-bg-loadgen")
    d = ld["data"]
    points_per_s = int(d["WORKERS"]) * int(d["DATAPOINTS_PER_REQ"]) * 1000 / int(d["INTERVAL_MS"])
    return {
        "logs": int(lg["data"]["LOGS_PER_MIN"]) / 60 * bytes_per_record(lg) / 1e6,
        "metrics": points_per_s * bytes_per_record(ld) / 1e6,
    }


def solve(docs: list[dict], bg: Background) -> list[dict]:
    """The manifests with the knobs set for `bg`. INTERVAL_MS is an integer, so the metrics rate lands within
    its rounding of the target; `rates` reports what the knobs give."""
    lg, ld = _configmap(docs, "otb-bg-loggen"), _configmap(docs, "otb-bg-loadgen")
    logs_per_s = bg.mb_per_s * bg.logs_share * 1e6 / bytes_per_record(lg)
    lg["data"]["LOGS_PER_MIN"] = str(round(logs_per_s * 60))
    points_per_s = bg.mb_per_s * (1 - bg.logs_share) * 1e6 / bytes_per_record(ld)
    d = ld["data"]
    per_request = int(d["WORKERS"]) * int(d["DATAPOINTS_PER_REQ"])
    d["INTERVAL_MS"] = str(max(1, round(per_request * 1000 / points_per_s))) if points_per_s > 0 else "0"
    return docs


def manifests(scenario: Scenario | None, repo: Path = REPO, source_repo: str | None = None) -> list[dict]:
    """What the capture applies: the committed manifests, solved for the scenario's background when it has one,
    with the generators' source repository replaced when `source_repo` is given (kind serves the local clone)."""
    docs = read_manifests(repo)
    if scenario is not None and scenario.background is not None:
        docs = solve(docs, scenario.background)
    if source_repo:
        _configmap(docs, "otb-bg-source")["data"]["OTB_BG_REPO"] = source_repo
    return docs


def deployments(docs: list[dict]) -> list[str]:
    return [d["metadata"]["name"] for d in docs if d["kind"] == "Deployment"]


def apply(docs: list[dict], context: str | None = None) -> list[str]:
    """Apply everything and wait for every generator's build and rollout; returns the Deployments waited for."""
    subprocess.run(_kubectl(context, "apply", "-f", "-"), input=yaml.safe_dump_all(docs), text=True, check=True)
    names = deployments(docs)
    for name in names:
        subprocess.run(
            _kubectl(context, "rollout", "status", f"deployment/{name}", "-n", NAMESPACE, "--timeout", ROLLOUT_TIMEOUT),
            check=True,
        )
    return names


KEPT_ON_REMOVE = ("Namespace", "PersistentVolumeClaim")


def remove(docs: list[dict], context: str | None = None) -> None:
    """Delete everything but the Namespace (it carries the runner's RoleBinding and NetworkPolicies) and the
    generators' cache claims (their next build would start from nothing); the generators stop exporting the
    moment their pods go."""
    rest = [d for d in docs if d["kind"] not in KEPT_ON_REMOVE]
    argv = _kubectl(context, "delete", "--ignore-not-found", "--wait", "-f", "-")
    subprocess.run(argv, input=yaml.safe_dump_all(rest), text=True, check=True)


def pod_problems(repo: Path = REPO) -> list[str]:
    """What doctor refuses in otb-bg: a pod that is not Guaranteed or an image without a digest. The pods are
    outside the per-stack budget sums: the background is load, not a system under test."""
    from otb.stacks.k8s import PINNED_IMAGE, pod_templates, qos_guaranteed

    problems = []
    for pod in pod_templates(NAMESPACE, read_manifests(repo)):
        for c in pod.containers + pod.init_containers:
            if not PINNED_IMAGE.match(str(c.get("image", ""))):
                problems.append(
                    f"{pod.where}: container {c.get('name')}: image {c.get('image')!r} is not pinned by digest"
                )
        bad = [c.get("name") for c in pod.containers + pod.init_containers if qos_guaranteed(c) is None]
        if bad:
            problems.append(f"{pod.where}: not Guaranteed (requests must equal limits for cpu and memory): {bad}")
    return problems
