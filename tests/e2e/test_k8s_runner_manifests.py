"""deploy/k8s/runner/ read as Kubernetes would, without a cluster: the namespaces, RBAC and NetworkPolicies that
generate.py derives from the stacks and the k8s scenarios, and the runner pod on public images.

Ways this could be wrong, each asserted below:
- a committed generated file is stale (generate.py --check);
- an instance of a `runtime: k8s` scenario has no namespace, no RoleBinding for the runner, no delete right in the
  namespaces ClusterRole, or no NetworkPolicies, so a capture of that scenario cannot install or restart its demo;
- a demo namespace lets its collector reach anything but the gateway (otb/otb-runner:4318), DNS, the namespace and
  the kubelet (NODE_CIDR:10250), or lets anything but the runner in, or the runner in anywhere but flagd 4000/8016
  and load-generator 8089;
- a stack namespace lets anything but the runner in, or its setup Jobs cannot reach the API server;
- the runner's egress misses the model API, the API server or an otb* namespace, or the gateway accepts OTLP from
  outside them;
- the pod runs an image that is not pinned by digest or not in charts.lock.yaml, or a built image; a container is
  not restricted; tools or agent mount the PVC root, .otb/secrets, suites, corpora, runs or the API token; agent
  mounts the stack secrets; the init container does not read the install ConfigMap;
- the install ConfigMap drifts from install.sh or pins.yaml, a pin is not a sha256, or the pinned Claude Code
  version differs from the one the kind test expects; the fixture ConfigMap drifts from the server or its
  requirements, or a requirement lacks hashes.

Evidence: tests/e2e/artifacts/k8s_runner_manifests.json.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml
from conftest import REPO, write_artifact

RUNNER = REPO / "deploy/k8s/runner"
LOCK = yaml.safe_load((REPO / "deploy/k8s/charts.lock.yaml").read_text())
PINNED = re.compile(r"^[^@\s]+:[^@\s]+@sha256:[0-9a-f]{64}$")


def docs(name: str) -> list[dict]:
    return [d for d in yaml.safe_load_all((RUNNER / name).read_text()) if d]


def by(kind: str, items: list[dict]) -> dict[str, dict]:
    return {d["metadata"]["name"]: d for d in items if d["kind"] == kind}


def policies(namespace: str) -> dict[str, dict]:
    return {
        d["metadata"]["name"]: d["spec"]
        for d in docs("networkpolicies.yaml")
        if d["kind"] == "NetworkPolicy" and d["metadata"]["namespace"] == namespace
    }


def k8s_instances() -> list[str]:
    out = set()
    for p in (REPO / "scenarios").glob("*.yaml"):
        doc = yaml.safe_load(p.read_text()) or {}
        if doc.get("runtime") == "k8s":
            out.update(doc["instances"])
    return sorted(out)


FROM_RUNNER = {
    "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "otb"}},
    "podSelector": {"matchLabels": {"app": "otb-runner"}},
}
DNS_RULE = {
    "to": [
        {
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
            "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
        }
    ],
    "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
}
KUBE_API_RULE = {
    "to": [{"ipBlock": {"cidr": "KUBE_API_CIDR"}}],
    "ports": [{"protocol": "TCP", "port": 6443}, {"protocol": "TCP", "port": 443}],
}


def deny_all_present(ns: str) -> None:
    p = policies(ns)
    assert p["default-deny"] == {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}, ns
    assert p["allow-dns"] == {"podSelector": {}, "policyTypes": ["Egress"], "egress": [DNS_RULE]}, ns
    assert p["allow-intra-namespace"] == {
        "podSelector": {},
        "policyTypes": ["Ingress", "Egress"],
        "ingress": [{"from": [{"podSelector": {}}]}],
        "egress": [{"to": [{"podSelector": {}}]}],
    }, ns


def test_generated_runner_files_match_their_sources() -> None:
    p = subprocess.run([sys.executable, str(RUNNER / "generate.py"), "--check"], capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr


def test_every_k8s_scenario_instance_has_namespace_rbac_and_policies(artifacts: Path) -> None:
    instances = k8s_instances()
    assert instances == ["eu1", "k1"], "scenarios/k8s-smoke.yaml and scenarios/tb-hybrid.yaml"
    stacks = ["otb-b1", "otb-b3", "otb-b4"]
    demos = [f"otb-demo-{i}" for i in instances]
    every = [*stacks, "otb-bg", "otb-fx", *demos]

    namespaces = by("Namespace", docs("namespaces-stacks.yaml"))
    assert sorted(namespaces) == sorted(every)
    level = {n: namespaces[n]["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] for n in every}
    assert level == {
        **dict.fromkeys([*stacks, *demos], "baseline"),
        **dict.fromkeys(["otb-bg", "otb-fx"], "restricted"),
    }, "the stacks run root as compose does and the demo chart sets no restricted fields; the rest is restricted"

    rbac = docs("rbac.yaml")
    bindings = [d["metadata"]["namespace"] for d in rbac if d["kind"] == "RoleBinding"]
    assert sorted(bindings) == sorted(["otb", *every])
    assert all(d["roleRef"]["name"] == "otb-runner-stacks" for d in rbac if d["kind"] == "RoleBinding")
    namespaces_role = by("ClusterRole", rbac)["otb-runner-namespaces"]
    deletable = next(r for r in namespaces_role["rules"] if r["resources"] == ["namespaces"] and "delete" in r["verbs"])
    assert deletable == {
        "apiGroups": [""],
        "resources": ["namespaces"],
        "resourceNames": every,
        "verbs": ["delete", "patch", "update"],
    }, "delete and the apply-time patch of a namespace, by name only"
    cluster_rbac = [r for r in namespaces_role["rules"] if r["resources"] == ["clusterroles", "clusterrolebindings"]]
    assert cluster_rbac == [
        {
            "apiGroups": ["rbac.authorization.k8s.io"],
            "resources": ["clusterroles", "clusterrolebindings"],
            "verbs": ["get", "list", "watch", "create"],
        },
        {
            "apiGroups": ["rbac.authorization.k8s.io"],
            "resources": ["clusterroles", "clusterrolebindings"],
            "resourceNames": [f"{ns}-kubeletstats" for ns in demos],
            "verbs": ["get", "update", "patch", "delete"],
        },
    ], "the demo's kubeletstats ClusterRole per instance, and nothing else cluster-wide"
    assert {"apiGroups": [""], "resources": ["nodes/stats"], "verbs": ["get"]} in namespaces_role["rules"]

    for ns in demos:
        deny_all_present(ns)
        p = policies(ns)
        assert p["allow-to-otb-gateway"] == {
            "podSelector": {},
            "policyTypes": ["Egress"],
            "egress": [{"to": [FROM_RUNNER], "ports": [{"protocol": "TCP", "port": 4318}]}],
        }
        assert p["allow-collector-to-kubelet"] == {
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": "opentelemetry-collector"}},
            "policyTypes": ["Egress"],
            "egress": [{"to": [{"ipBlock": {"cidr": "NODE_CIDR"}}], "ports": [{"protocol": "TCP", "port": 10250}]}],
        }
        assert p["allow-runner-to-flagd"] == {
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": "flagd"}},
            "policyTypes": ["Ingress"],
            "ingress": [
                {"from": [FROM_RUNNER], "ports": [{"protocol": "TCP", "port": 4000}, {"protocol": "TCP", "port": 8016}]}
            ],
        }
        assert p["allow-runner-to-load-generator"] == {
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": "load-generator"}},
            "policyTypes": ["Ingress"],
            "ingress": [{"from": [FROM_RUNNER], "ports": [{"protocol": "TCP", "port": 8089}]}],
        }
        assert sorted(p) == [
            "allow-collector-to-kubelet", "allow-dns", "allow-intra-namespace", "allow-runner-to-flagd",
            "allow-runner-to-load-generator", "allow-to-otb-gateway", "default-deny",
        ], "nothing else is allowed"  # fmt: skip
    for ns in stacks:
        deny_all_present(ns)
        p = policies(ns)
        assert p["allow-from-runner"] == {
            "podSelector": {},
            "policyTypes": ["Ingress"],
            "ingress": [{"from": [FROM_RUNNER]}],
        }
        assert p["allow-setup-to-kube-api"] == {
            "podSelector": {"matchLabels": {"otb.role": "setup"}},
            "policyTypes": ["Egress"],
            "egress": [KUBE_API_RULE],
        }
        assert sorted(p) == [
            "allow-dns",
            "allow-from-runner",
            "allow-intra-namespace",
            "allow-setup-to-kube-api",
            "default-deny",
        ]

    runner = policies("otb")
    in_every = {
        "namespaceSelector": {
            "matchExpressions": [{"key": "kubernetes.io/metadata.name", "operator": "In", "values": every}]
        }
    }
    assert runner["otb-runner-egress"]["egress"] == [
        {
            "to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]}}],
            "ports": [{"protocol": "TCP", "port": 443}],
        },
        KUBE_API_RULE,
        {"to": [in_every]},
    ]
    assert runner["otb-gateway-ingress"]["ingress"] == [
        {"from": [in_every], "ports": [{"protocol": "TCP", "port": 4318}]}
    ]
    write_artifact(
        artifacts / "k8s_runner_manifests.json",
        {
            "instances": instances,
            "namespaces": {n: level[n] for n in every},
            "role_bindings": sorted(bindings),
            "network_policies": {ns: sorted(policies(ns)) for ns in ["otb", *every] if policies(ns)},
        },
    )


def mounts(c: dict) -> dict[str, dict]:
    return {m["mountPath"]: m for m in c.get("volumeMounts", [])}


def test_runner_pod_runs_pinned_public_images_with_the_split_kept() -> None:
    (sts,) = [d for d in docs("statefulset.yaml") if d["kind"] == "StatefulSet"]
    spec = sts["spec"]["template"]["spec"]
    assert spec["automountServiceAccountToken"] is False
    assert spec["securityContext"] == {
        "runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001,
        "fsGroupChangePolicy": "OnRootMismatch", "seccompProfile": {"type": "RuntimeDefault"},
    }  # fmt: skip
    containers = {c["name"]: c for c in spec["initContainers"] + spec["containers"]}
    assert list(containers) == ["install", "runner", "tools", "agent"]
    pins = yaml.safe_load((RUNNER / "pins.yaml").read_text())
    images = {n: c["image"] for n, c in containers.items()}
    assert images == {
        "install": pins["images"]["runner"],
        "runner": pins["images"]["runner"],
        "tools": pins["images"]["tools"],
        "agent": pins["images"]["agent"],
    }
    for name, image in images.items():
        assert PINNED.match(image) and image in LOCK["images"], f"{name}: {image} pinned and in charts.lock.yaml"
    built = [p.name for p in RUNNER.glob("*.yaml") if "otb-runner:local" in p.read_text()]
    assert built == [], "no manifest runs a built image"
    (fixture,) = [d for d in docs("fixture-stack.yaml") if d["kind"] == "Deployment"]
    fixture_images = {
        c["image"]
        for c in fixture["spec"]["template"]["spec"]["containers"]
        + fixture["spec"]["template"]["spec"]["initContainers"]
    }
    assert fixture_images == {pins["images"]["agent"]}
    for name, c in containers.items():
        assert c["securityContext"] == {
            "allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}, "readOnlyRootFilesystem": True
        }, name  # fmt: skip
        assert c["resources"]["requests"] == c["resources"]["limits"], f"{name}: Guaranteed"

    pvc = {n: {p: m for p, m in mounts(c).items() if m["name"] == "work"} for n, c in containers.items()}
    assert set(pvc["install"]) == {"/work"} and set(pvc["runner"]) == {"/work"}, (
        "only runner and the installer see the PVC root"
    )
    assert {p: m["subPath"] for p, m in pvc["tools"].items()} == {
        "/work/tools": "tools",
        "/work/app/otb": "app/otb",
        "/work/app/.venv": "app/.venv",
        "/work/app/servers": "app/servers",
        "/work/app/tests/fixtures": "app/tests/fixtures",
        "/work/app/.otb/servers": "app/.otb/servers",
        "/work/app/.otb/bin": "app/.otb/bin",
        "/work/app/.otb/venvs": "app/.otb/venvs",
        "/work/app/.otb/node": "app/.otb/node",
    }
    assert {p: m["subPath"] for p, m in pvc["agent"].items()} == {
        "/work/tools": "tools",
        "/work/app/otb": "app/otb",
        "/work/app/.venv": "app/.venv",
    }
    assert all(m["readOnly"] for n in ("tools", "agent") for m in pvc[n].values())
    token = "/var/run/secrets/kubernetes.io/serviceaccount"
    assert {n: token in mounts(c) for n, c in containers.items()} == {
        "install": False, "runner": True, "tools": False, "agent": False
    }  # fmt: skip
    assert {n: "/secrets" in mounts(c) for n, c in containers.items()}["tools"] and "/secrets" not in mounts(
        containers["agent"]
    )
    assert "/credentials" in mounts(containers["agent"]) and "/credentials" not in mounts(containers["tools"])
    assert containers["install"]["envFrom"] == [{"configMapRef": {"name": "otb-runner-install"}}]
    runner_env = {e["name"]: e.get("value") for e in containers["runner"]["env"]}
    assert runner_env["OTB_RUNTIME"] == "k8s" and runner_env["OTB_ISOLATION"] == "split", (
        "every otb verb in the pod loads the stacks under the k8s runtime and dispatches split"
    )
    assert mounts(containers["install"])["/install"] == {"name": "install", "mountPath": "/install", "readOnly": True}
    assert mounts(containers["runner"])["/cells"]["name"] == mounts(containers["agent"])["/cells"]["name"] == "cells"


def test_install_and_fixture_configmaps_restate_their_files() -> None:
    (install,) = docs("install-configmap.yaml")
    data = install["data"]
    assert data["install.sh"] == (RUNNER / "install.sh").read_text()
    pins = yaml.safe_load((RUNNER / "pins.yaml").read_text())
    for tool in ("uv", "kubectl", "helm", "claude"):
        assert data[f"{tool.upper()}_VERSION"] == str(pins[tool]["version"])
        for arch in ("arm64", "amd64"):
            sha = data[f"{tool.upper()}_SHA256_{arch.upper()}"]
            assert re.fullmatch(r"[0-9a-f]{64}", sha), (tool, arch)
            assert sha == pins[tool]["sha256"][arch]
    assert data["PYTHON_VERSIONS"].split() == [str(v) for v in pins["python"]["versions"].values()]
    assert pins["claude"]["version"] == "2.1.295"

    (fixture,) = docs("fixture-configmap.yaml")
    assert fixture["metadata"]["namespace"] == "otb-fx"
    assert fixture["data"]["mcp_fixture_server.py"] == (REPO / "tests/fixtures/mcp_fixture_server.py").read_text()
    requirements = (RUNNER / "fixture-requirements.txt").read_text()
    assert fixture["data"]["requirements.txt"] == requirements
    pinned = [ln for ln in requirements.splitlines() if "==" in ln and not ln.startswith("#")]
    assert len(pinned) >= 20 and all(ln.endswith("\\") for ln in pinned), "every requirement carries hashes"
    assert "mcp==2.2.0" in requirements
