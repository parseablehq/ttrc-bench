"""deploy/k8s/demo renders, for both profiles, an instance whose every pod is Guaranteed and every image pinned by
digest, that reproduces the Compose capture's services, environment, ports and image digests (and on gke its memory
limits), and whose collector sends everything to the TTRC-Bench gateway labelled with the instance
(tests/k8s/demo_check.py lists each way this can go wrong). Two instances render without a cluster-scoped name in
common.

Needs helm and the pinned chart in .otb/charts (deploy/k8s/demo/install.sh pulls it); skips without them.
Evidence: tests/e2e/artifacts/demo_k8s_render.json, with each profile's per-instance cpu and memory.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from conftest import write_artifact

REPO = Path(__file__).resolve().parents[2]
CHART = REPO / ".otb" / "charts" / "opentelemetry-demo-0.42.3.tgz"
sys.path.insert(0, str(REPO / "tests" / "k8s"))
import demo_check  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None or not CHART.exists(), reason="needs helm and the pinned chart in .otb/charts"
)


def render(instance: str, profile: str) -> list[dict]:
    argv = [str(REPO / "deploy/k8s/demo/install.sh"), "--template", instance, profile]
    out = subprocess.run(argv, capture_output=True, text=True, check=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def totals(docs: list[dict]) -> dict[str, float]:
    cpu = mem = 0.0
    for d in docs:
        if d["kind"] == "Deployment":
            for c in d["spec"]["template"]["spec"]["containers"]:
                cpu += demo_check.quantity(c["resources"]["limits"]["cpu"])
                mem += demo_check.quantity(c["resources"]["limits"]["memory"])
    return {"cpu_cores": round(cpu, 3), "memory_gib": round(mem / (1 << 30), 3)}


def test_rendered_instances_are_guaranteed_pinned_and_match_compose(artifacts: Path) -> None:
    compose = json.loads(demo_check.COMPOSE.read_text())
    source = yaml.safe_load(demo_check.SOURCE.read_text())
    result = {}
    for profile in ("gke", "kind"):
        docs = render("eu1", profile)
        result[profile] = {
            "problems": demo_check.check(docs, "eu1", profile, compose, source),
            "deployments": sorted(d["metadata"]["name"] for d in docs if d["kind"] == "Deployment"),
            "per_instance": totals(docs),
        }
    scoped = ("ClusterRole", "ClusterRoleBinding")
    cluster_scoped = {
        i: sorted(d["metadata"]["name"] for d in render(i, "kind") if d["kind"] in scoped) for i in ("eu1", "us2")
    }
    result["cluster_scoped"] = cluster_scoped
    write_artifact(artifacts / "demo_k8s_render.json", result)

    services = sorted(set(compose) - {"flagd-ui"})
    assert result == {
        "gke": {"problems": [], "deployments": services, "per_instance": {"cpu_cores": 4.1, "memory_gib": 5.308}},
        "kind": {"problems": [], "deployments": services, "per_instance": {"cpu_cores": 1.73, "memory_gib": 3.69}},
        "cluster_scoped": {
            "eu1": ["otb-demo-eu1-kubeletstats", "otb-demo-eu1-kubeletstats"],
            "us2": ["otb-demo-us2-kubeletstats", "otb-demo-us2-kubeletstats"],
        },
    }
