"""deploy/k8s/background/ read as Kubernetes would, without a cluster: the contract the background unit promises.

Ways the manifests can be wrong, each asserted below:
- a pod is not Guaranteed QoS: some container or init container lacks cpu and memory requests equal to limits;
- an image is not pinned by digest;
- an object lands outside namespace otb-bg;
- a generator runs the default field profile instead of the generic one;
- the source pin is a placeholder rather than a 40-hex commit of github.com/parseablehq/otel-benchmark;
- a collector pipeline does not stamp cloud.region=bg and service.instance.id=bg, or does not export OTLP/HTTP
  JSON to otb-gateway.otb.svc.cluster.local:4318;
- a generator sends anywhere but the collector Service;
- a pod runs with a service-account token, as root, with a writable root filesystem or with capabilities;
- the rate knobs do not produce scenarios/tb-hybrid.yaml's background mb_per_s and logs_share (2% tolerance);
- a generator's build has no claim to keep its caches and binary on, or mounts it anywhere but /work.

Writes tests/e2e/artifacts/background_manifests.json.
"""

import re
from pathlib import Path

import yaml
from background import manifests, target_mb_per_s
from conftest import REPO, write_artifact

from otb import background
from otb.scenario import Background, load_scenario


def served_deployment(docs: list[dict], name: str) -> dict:
    (d,) = [d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == name]
    return d["spec"]["template"]["spec"]


def by(kind: str, items: list[dict]) -> dict[str, dict]:
    return {d["metadata"]["name"]: d for d in items if d["kind"] == kind}


def pod_problems(name: str, spec: dict) -> list[str]:
    out = []
    if spec.get("automountServiceAccountToken") is not False:
        out.append(f"{name}: service-account token mounted")
    if not spec.get("securityContext", {}).get("runAsNonRoot"):
        out.append(f"{name}: runAsNonRoot not set")
    for c in spec.get("initContainers", []) + spec["containers"]:
        r = c.get("resources", {})
        for res in ("cpu", "memory"):
            if r.get("requests", {}).get(res) is None or r.get("requests", {}).get(res) != r.get("limits", {}).get(res):
                out.append(f"{name}/{c['name']}: {res} request != limit")
        if not re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", c["image"]):
            out.append(f"{name}/{c['name']}: image {c['image']} not pinned by digest")
        sc = c.get("securityContext", {})
        if not (sc.get("readOnlyRootFilesystem") and sc.get("allowPrivilegeEscalation") is False):
            out.append(f"{name}/{c['name']}: writable root or privilege escalation")
        if sc.get("capabilities", {}).get("drop") != ["ALL"]:
            out.append(f"{name}/{c['name']}: capabilities not dropped")
    return out


def test_background_manifests(artifacts: Path) -> None:
    m = manifests()
    items = list(m.values())
    cms, deps = by("ConfigMap", items), by("Deployment", items)
    collector = yaml.safe_load(cms["otb-bg-collector"]["data"]["config.yaml"])
    exporters = collector["exporters"]
    coll = deps["otb-bg-collector"]["spec"]["template"]["spec"]["containers"][0]
    gateway_url = next(e["value"] for e in coll["env"] if e["name"] == "OTB_GATEWAY")

    pipelines = {}
    for sig, p in collector["service"]["pipelines"].items():
        stamps = set()
        for proc in p["processors"]:
            for a in collector["processors"][proc].get("attributes", []):
                if a["action"] == "upsert":
                    stamps.add(f"{a['key']}={a['value']}")
        pipelines[sig] = {
            "stamps": sorted(stamps),
            "exports": sorted(
                f"{exporters[e]['encoding']} {exporters[e]['endpoint'].replace('${env:OTB_GATEWAY}', gateway_url)}"
                for e in p["exporters"]
            ),
        }

    src = cms["otb-bg-source"]["data"]
    lg, ld = cms["otb-bg-loggen"]["data"], cms["otb-bg-loadgen"]["data"]
    target = target_mb_per_s(m)
    bg = load_scenario(REPO / "scenarios/tb-hybrid.yaml").background
    assert bg is not None
    want = {"logs": bg.mb_per_s * bg.logs_share, "metrics": bg.mb_per_s * (1 - bg.logs_share)}

    result = {
        "namespaces": sorted({d["metadata"].get("namespace", d["metadata"]["name"]) for d in items}),
        "kinds": sorted(f"{d['kind']}/{d['metadata']['name']}" for d in items),
        "pod_problems": [p for n, d in deps.items() for p in pod_problems(n, d["spec"]["template"]["spec"])],
        "source_repo": src["OTB_BG_REPO"],
        "source_commit_is_sha": bool(re.fullmatch(r"[0-9a-f]{40}", src["OTB_BG_COMMIT"])),
        "profiles": {n: cms[f"otb-bg-{n}"]["data"]["FIELD_PROFILE"] for n in ("loggen", "loadgen")},
        "generator_endpoints": sorted({lg["OTEL_ENDPOINT"], ld["OTEL_ENDPOINT"]}),
        "collector_service_port": [p["port"] for p in by("Service", items)["otb-bg-collector"]["spec"]["ports"]],
        "pipelines": pipelines,
        "mb_per_s_within_2pct": {s: abs(target[s] - want[s]) <= 0.02 * want[s] for s in want},
    }
    write_artifact(artifacts / "background_manifests.json", result)

    gateway = "json http://otb-gateway.otb.svc.cluster.local:4318"
    stamps = ["cloud.region=bg", "service.instance.id=bg"]
    assert result == {
        "namespaces": ["otb-bg"],
        "kinds": [
            "ConfigMap/otb-bg-build",
            "ConfigMap/otb-bg-collector",
            "ConfigMap/otb-bg-loadgen",
            "ConfigMap/otb-bg-loggen",
            "ConfigMap/otb-bg-source",
            "Deployment/otb-bg-collector",
            "Deployment/otb-bg-loadgen",
            "Deployment/otb-bg-loggen",
            "Namespace/otb-bg",
            "NetworkPolicy/allow-dns",
            "NetworkPolicy/collector",
            "NetworkPolicy/default-deny-egress",
            "NetworkPolicy/generators",
            "PersistentVolumeClaim/otb-bg-loadgen-cache",
            "PersistentVolumeClaim/otb-bg-loggen-cache",
            "Service/otb-bg-collector",
        ],
        "pod_problems": [],
        "source_repo": "https://github.com/parseablehq/otel-benchmark.git",
        "source_commit_is_sha": True,
        "profiles": {"loggen": "generic", "loadgen": "generic"},
        "generator_endpoints": ["http://otb-bg-collector:4318"],
        "collector_service_port": [4318, 8888],
        "pipelines": {
            "logs": {"stamps": stamps, "exports": [gateway]},
            "metrics": {"stamps": stamps, "exports": [gateway]},
        },
        "mb_per_s_within_2pct": {"logs": True, "metrics": True},
    }


def test_capture_solves_the_rate_knobs_for_a_scenario(artifacts: Path) -> None:
    """What `otb capture --decoy-driver k8s` applies: the committed manifests with LOGS_PER_MIN and INTERVAL_MS
    solved for the scenario's background. Ways this fails: the solved knobs miss the scenario's rate by more than
    the integer rounding of INTERVAL_MS; the committed knobs are not tb-hybrid's; a scenario without a background
    changes the files; the source override does not reach the build ConfigMap; a Namespace is removed with the
    rest."""
    committed = background.rates(background.manifests(None))
    tb = load_scenario(REPO / "scenarios/tb-hybrid.yaml")
    small = Background(0.4, 0.6)
    solved = {
        "tb-hybrid": background.rates(background.manifests(tb)),
        "laptop": background.rates(background.solve(background.read_manifests(), small)),
    }
    want = {
        "tb-hybrid": {"logs": tb.background.mb_per_s * 0.6, "metrics": tb.background.mb_per_s * 0.4},  # type: ignore[union-attr]
        "laptop": {"logs": 0.24, "metrics": 0.16},
    }
    off = {k: {s: abs(solved[k][s] - want[k][s]) / want[k][s] for s in want[k]} for k in want}
    assert all(v <= 0.02 for d in off.values() for v in d.values()), off
    assert committed == background.rates(background.read_manifests()), "no background: the files as committed"
    knobs = {
        d["metadata"]["name"]: {k: d["data"][k] for k in ("LOGS_PER_MIN", "INTERVAL_MS") if k in d["data"]}
        for d in background.solve(background.read_manifests(), small)
        if d["kind"] == "ConfigMap" and d["metadata"]["name"] in ("otb-bg-loggen", "otb-bg-loadgen")
    }
    assert knobs == {"otb-bg-loggen": {"LOGS_PER_MIN": "13649"}, "otb-bg-loadgen": {"INTERVAL_MS": "4712"}}
    served = background.manifests(tb, source_repo="git://host.docker.internal:9418/otel-benchmark-otb")
    source = next(d for d in served if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "otb-bg-source")
    assert source["data"]["OTB_BG_REPO"] == "git://host.docker.internal:9418/otel-benchmark-otb"
    assert source["data"]["OTB_BG_COMMIT"] == "4f7163f3046c02e63bc6e742b1393fe3632ee298", "the pin stays"
    assert background.deployments(served) == ["otb-bg-collector", "otb-bg-loadgen", "otb-bg-loggen"]
    for g in ("loggen", "loadgen"):
        spec = served_deployment(served, f"otb-bg-{g}")
        work = next(v for v in spec["volumes"] if v["name"] == "work")
        assert work == {"name": "work", "persistentVolumeClaim": {"claimName": f"otb-bg-{g}-cache"}}
        build = spec["initContainers"][0]
        assert {"name": "work", "mountPath": "/work"} in build["volumeMounts"]
    assert "GOTMPDIR=/work/tmp" in manifests()["ConfigMap", "otb-bg-build"]["data"]["build.sh"], (
        "a read-only root needs it"
    )
    write_artifact(artifacts / "background_rates.json", {"committed": committed, "solved": solved, "off": off})
