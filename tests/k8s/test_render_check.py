"""Offline when the charts are bootstrapped: the stacks rendered as `otb up --runtime k8s` renders them pass
tests/k8s/check_rendered.py on both profiles, against the compose profile each restates. Skips before
`otb bootstrap --runtime k8s`, since rendering a chart needs its archive. Always: the checker refuses a backend
or ingest container that carries a livenessProbe, the rule the no-liveness post-renderer exists for.

Evidence: tests/k8s/artifacts/render-check.json."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CHECK = REPO / "tests/k8s/check_rendered.py"
PROFILES = {"kind": "profiles/laptop.env", "gke": "profiles/bighost.env"}


def bootstrapped() -> bool:
    from otb.stacks.definition import registry
    from otb.stacks.k8s import chart_archive, chart_releases

    stacks = list(registry(REPO, "k8s").values())
    return all(chart_archive(r, REPO).exists() for r in chart_releases(stacks))


@pytest.mark.skipif(not bootstrapped(), reason="needs the charts from `otb bootstrap --runtime k8s`")
def test_rendered_stacks_pass_the_checker() -> None:
    out: dict[str, list[str]] = {}
    for profile, env in PROFILES.items():
        p = subprocess.run(
            [sys.executable, str(CHECK), "--profile", profile, "--env", env], capture_output=True, text=True, cwd=REPO
        )
        out[profile] = p.stdout.splitlines()
        assert p.returncode == 0, p.stdout + p.stderr
        assert out[profile][-1].endswith(": ok")
    if os.environ.get("OTB_WRITE_ARTIFACTS") == "1":
        (REPO / "tests/k8s/artifacts/render-check.json").write_text(json.dumps(out, indent=1) + "\n")


def test_checker_refuses_a_liveness_probe_on_a_backend(tmp_path: Path) -> None:
    doc = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "store"},
        "spec": {
            "replicas": 1,
            "template": {
                "metadata": {"labels": {"otb.stack": "b9-store", "otb.role": "backend"}},
                "spec": {
                    "containers": [
                        {
                            "name": "store",
                            "image": "prom/prometheus:v3.15.0@sha256:"
                            "efd719c99d83b060d9daefdcf00360461adf279f45ef5391f8d111892118753e",
                            "resources": {
                                "requests": {"cpu": "1", "memory": "1Gi"},
                                "limits": {"cpu": "1", "memory": "1Gi"},
                            },
                            "livenessProbe": {"httpGet": {"path": "/-/healthy", "port": 9090}},
                            "readinessProbe": {"httpGet": {"path": "/-/ready", "port": 9090}},
                        }
                    ]
                },
            },
        },
    }
    import yaml

    rendered = tmp_path / "rendered.yaml"
    rendered.write_text(yaml.safe_dump(doc))
    p = subprocess.run([sys.executable, str(CHECK), str(rendered)], capture_output=True, text=True, cwd=REPO)
    assert p.returncode == 1
    assert "FAIL Deployment/store container store: livenessProbe on a backend container" in p.stdout
    del doc["spec"]["template"]["spec"]["containers"][0]["livenessProbe"]
    rendered.write_text(yaml.safe_dump(doc))
    p = subprocess.run([sys.executable, str(CHECK), str(rendered)], capture_output=True, text=True, cwd=REPO)
    assert "livenessProbe" not in p.stdout, "the readiness probe alone is allowed"
