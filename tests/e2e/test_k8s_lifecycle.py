"""The Kubernetes runtime of `otb up`, `down`, `bootstrap` and `doctor`, driven over a copy of
tests/fixtures/k8s-repo with a fake `helm` and `kubectl` on PATH (tests/fixtures/fake_helm.py, fake_kubectl.py)
and a local HTTP server standing in for the stacks' readiness endpoints and the MCP release download. No cluster.

Ways the runtime could fail, each asserted below:
- an endpoint override leaks into a compose load, fails to apply under k8s, or an unknown dotted path is ignored;
- releases are applied in another order than stack.yaml lists them, a manifests release goes to helm, the budget
  overlay is passed before the release's own values (so the stack would override the budget), or the credential
  Secret is applied after the releases that reference it;
- bootstrap accepts a chart archive whose sha256 differs from charts.lock.yaml, pulls a chart twice, or installs
  a release binary whose archive sha256 differs from its lockfile;
- up leaves a release that an interrupted run left pending (helm then refuses every upgrade), applies a Recreate
  strategy over a live RollingUpdate Deployment (the API server refuses it), returns before a workload is rolled
  out or a setup Job complete, or passes a failed Job as success;
- down removes releases in apply order, deletes PVCs without --volumes, or forgets record_wipe with it;
- doctor passes a Burstable pod, an image without a digest, unequal per-role budgets, or an unlabelled pod, or
  fails the good rendering; or passes a background pod (otb-bg) that is Burstable or unpinned, or counts one
  in a stack's budget;
- a recorded 127.0.0.1 probe no longer replays once the stack's URLs are cluster addresses, or a recording from
  an unrelated host replays as if it were the stack's;
- a k8s endpoint names a Service that drops a busy pod (no publishNotReadyAddresses), so the gate or the tools
  container gets connection refused while a backend is slow.

Evidence: tests/e2e/artifacts/k8s_lifecycle.json.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml
from conftest import REPO, free_port, otb, write_artifact

from otb.gate import RESPONSES, append_response, read_responses, rejudge
from otb.stacks.definition import ManifestRelease, StackDefError, registry
from otb.stacks.k8s import chart_archive, chart_lock, chart_releases, locked, post_renderer_installed
from otb.stacks.lifecycle import bootstrap, budgets_table, doctor, down, install_binary, up
from otb.stacks.mcp import launch_vars, resolve_stdio

FIXTURE = REPO / "tests/fixtures/k8s-repo"
RENDER = REPO / "tests/fixtures/k8s-render"
GATE_SLICE = REPO / "tests/fixtures/gate-slice"
FX_MCP_SCRIPT = b'#!/bin/sh\necho "fx-mcp ok $*"\n'


def tarball(name: str, body: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name)
        info.size, info.mode = len(body), 0o755
        tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


@dataclass
class Harness:
    repo: Path
    port: int
    log: Path
    state_file: Path
    requests: list[str] = field(default_factory=list)

    def state(self, **tables: object) -> None:
        """What the fake kubectl and helm answer from: `history`, `deployments`, `jobs` (tests/fixtures/fake_*.py)."""
        self.state_file.write_text(json.dumps({"jobs": {"otb-k2/store-setup": "Complete"}} | tables))

    def calls(self) -> list[list]:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def reset(self) -> None:
        self.log.unlink(missing_ok=True)
        self.requests.clear()

    def short(self, call: list) -> list:
        """A call with the repo path made relative, for the committed artifact."""
        return [a.replace(str(self.repo), "<repo>") if isinstance(a, str) else a for a in call]


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    archive = tarball("fx-mcp", FX_MCP_SCRIPT)
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            body = archive if self.path == "/fx-mcp.tar.gz" else b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: object) -> None:
            pass

    port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE, repo, ignore=shutil.ignore_patterns("__pycache__"))
    lock = repo / "servers/fx/fx-mcp.lock.yaml"
    lock.write_text(
        lock.read_text().replace("__PORT__", str(port)).replace("__ASSET_SHA__", hashlib.sha256(archive).hexdigest())
    )
    lock_sha = hashlib.sha256(lock.read_bytes()).hexdigest()
    for sy in repo.glob("stacks/*/stack.yaml"):
        sy.write_text(sy.read_text().replace("__PORT__", str(port)).replace("__LOCK_SHA__", lock_sha))

    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    for tool in ("helm", "kubectl"):
        shim = stub_bin / tool
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{REPO}/tests/fixtures/fake_{tool}.py" "$@"\n')
        shim.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("PATH", f"{stub_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_RENDER", str(RENDER / "good"))
    monkeypatch.setenv("FAKE_STATE", str(tmp_path / "state.json"))
    monkeypatch.delenv("OTB_K8S_CONTEXT", raising=False)
    monkeypatch.delenv("OTB_RUNTIME", raising=False)
    h = Harness(repo, port, log, tmp_path / "state.json", requests)
    h.state()
    yield h
    server.shutdown()


def test_endpoint_overrides_apply_only_under_k8s(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, port = harness.repo, harness.port
    compose = registry(repo, "compose")
    k1, k2 = compose["k1-alpha"], compose["k2-beta"]
    assert (k1.runtime, k1.ingest.url, k1.ingest.metrics_url, k1.urls["store"], k1.readiness[0].url) == (
        "compose",
        "http://127.0.0.1:4391",
        "http://127.0.0.1:18891/metrics",
        "http://127.0.0.1:39100",
        f"http://127.0.0.1:{port}/k1/compose/ready",
    )
    assert k1.surface.mcp.env["STORE_URL"] == "http://127.0.0.1:39100"
    assert k2.surface.mcp.url == "http://127.0.0.1:39200/mcp"

    k8s = registry(repo, "k8s")
    k1, k2 = k8s["k1-alpha"], k8s["k2-beta"]
    assert (k1.runtime, k1.ingest.url, k1.ingest.metrics_url, k1.urls["store"], k1.readiness[0].url) == (
        "k8s",
        "http://ingest-k1.otb-k1.svc.cluster.local:4318",
        "http://ingest-k1.otb-k1.svc.cluster.local:8888/metrics",
        "http://store.otb-k1.svc.cluster.local:9100",
        f"http://127.0.0.1:{port}/k1/k8s/ready",
    )
    assert k1.surface.mcp.env["STORE_URL"] == "http://store.otb-k1.svc.cluster.local:9100"
    assert k1.surface.mcp.env["TOKEN"] == "${K1_TOKEN}", "a field with no override is untouched"
    assert k2.surface.mcp.url == "http://store.otb-k2.svc.cluster.local:9200/mcp"
    assert k1.k8s is not None and k1.k8s.namespace == "otb-k1"
    assert [r.name for r in k1.k8s.releases] == ["k1-store", "k1-ingest"]

    monkeypatch.setenv("OTB_RUNTIME", "k8s")
    assert registry(repo)["k1-alpha"].urls["store"] == "http://store.otb-k1.svc.cluster.local:9100"
    monkeypatch.setenv("OTB_RUNTIME", "swarm")
    with pytest.raises(StackDefError, match="OTB_RUNTIME='swarm': must be one of compose, k8s"):
        registry(repo)

    bad = repo / "stacks/k1-alpha/stack.yaml"
    bad.write_text(bad.read_text().replace("urls.store:", "urls.nope:"))
    with pytest.raises(StackDefError, match=r"k8s\.endpoints\.urls\.nope: not a URL-bearing field; known: ingest"):
        registry(repo, "k8s")
    with pytest.raises(StackDefError, match=r"k8s\.endpoints\.urls\.nope"):
        registry(repo, "compose")


def pull(chart: str, version: str) -> list[str]:
    repo = f"https://charts.fx.invalid/{chart}"
    return ["helm", "pull", chart, "--repo", repo, "--version", version, "--destination", "<repo>/.otb/charts"]


POST_RENDER = ("--post-renderer", "no-liveness")


def upgrade(release: str, archive: str, ns: str, values: str, *ctx: str) -> list[str]:
    overlay = f"<repo>/.otb/k8s/values/kind/{release}.yaml"
    argv = ["helm", *ctx, "upgrade", "--install", release, f"<repo>/.otb/charts/{archive}", "--namespace", ns]
    argv += ["--create-namespace", "-f", f"<repo>/deploy/k8s/stacks/{values}", "-f", overlay]
    return [*argv, *POST_RENDER, "--wait", "--wait-for-jobs", "--timeout", "15m"]


def chart_steps(release: str, archive: str, ns: str, values: str) -> list[list[str]]:
    """A chart release converges in three calls: render (for the strategy check), history (for a pending
    release), then the upgrade."""
    overlay = f"<repo>/.otb/k8s/values/kind/{release}.yaml"
    template = ["helm", "template", release, f"<repo>/.otb/charts/{archive}", "--namespace", ns]
    template += ["-f", f"<repo>/deploy/k8s/stacks/{values}", "-f", overlay, *POST_RENDER]
    history = ["helm", "history", release, "--namespace", ns, "-o", "json"]
    return [template, history, upgrade(release, archive, ns, values)]


def uninstall(release: str, ns: str, *ctx: str) -> list[str]:
    return ["helm", *ctx, "uninstall", release, "--namespace", ns, "--ignore-not-found", "--wait"]


def test_bootstrap_up_down_argv_and_order(harness: Harness, monkeypatch: pytest.MonkeyPatch, artifacts: Path) -> None:
    repo = harness.repo
    stacks = list(registry(repo, "k8s").values())
    charts = repo / ".otb/charts"
    evidence: dict[str, object] = {}

    bootstrap(stacks, "kind", repo)
    pulls = [c for c in harness.calls() if c[:2] == ["helm", "pull"]]
    assert [harness.short(c) for c in pulls] == [pull("store", "1.0.0"), pull("collector", "0.1.0")], (
        "each chart pulled once, from the lock's repo, into .otb/charts"
    )
    plugins = [harness.short(c) for c in harness.calls() if c[:2] == ["helm", "plugin"]]
    assert plugins == [
        ["helm", "plugin", "list"],
        ["helm", "plugin", "install", "<repo>/deploy/k8s/stacks/no-liveness"],
    ], "the no-liveness post-renderer is installed from the repo when missing"
    assert sorted(p.name for p in charts.iterdir()) == ["collector-0.1.0.tgz", "store-1.0.0.tgz"]
    binary = repo / ".otb/bin/fx-mcp"
    assert binary.stat().st_mode & stat.S_IXUSR
    assert subprocess.run([str(binary), "--stdio"], capture_output=True, text=True).stdout == "fx-mcp ok --stdio\n"
    assert harness.requests == ["/fx-mcp.tar.gz"]
    evidence["bootstrap"] = [harness.short(c) for c in harness.calls()]

    harness.reset()
    bootstrap(stacks, "kind", repo)
    assert harness.calls() == [["helm", "plugin", "list"]] and harness.requests == [], (
        "a second bootstrap pulls, downloads and installs nothing"
    )

    (charts / "store-1.0.0.tgz").unlink()
    monkeypatch.setenv("FAKE_CHART_SUFFIX", "tampered")
    with pytest.raises(
        StackDefError, match=r"k1-store: store-1\.0\.0\.tgz sha256 [0-9a-f]{64} differs from the lock's 2c84e9d2"
    ):
        bootstrap(stacks, "kind", repo)
    assert not (charts / "store-1.0.0.tgz").exists(), "the refused archive is removed"
    monkeypatch.delenv("FAKE_CHART_SUFFIX")
    bootstrap(stacks, "kind", repo)

    harness.reset()
    report = up(stacks, "kind", repo)
    calls = harness.calls()
    secret_k1 = calls[0].pop()
    secret_k2 = calls[7].pop()
    manifest = "<repo>/deploy/k8s/stacks/k2-beta/store-kind.yaml"
    assert [harness.short(c) for c in calls] == [
        ["kubectl", "apply", "-f", "-"],
        *chart_steps("k1-store", "store-1.0.0.tgz", "otb-k1", "k1-alpha/store.yaml"),
        *chart_steps("k1-ingest", "collector-0.1.0.tgz", "otb-k1", "k1-alpha/ingest.yaml"),
        ["kubectl", "apply", "-f", "-"],
        ["kubectl", "delete", "job", "store-setup", "--namespace", "otb-k2", "--ignore-not-found", "--wait"],
        ["kubectl", "apply", "--namespace", "otb-k2", "-f", manifest],
        ["kubectl", "rollout", "status", "statefulset/store", "--namespace", "otb-k2", "--timeout", "15m"],
        ["kubectl", "get", "job", "store-setup", "--namespace", "otb-k2", "-o", "json"],
        *chart_steps("k2-ingest", "collector-0.1.0.tgz", "otb-k2", "k2-beta/ingest.yaml"),
    ], (
        "namespace and Secret first, then the releases in stack.yaml order, the profile overlay after the values; "
        "a chart release waits through helm, a manifests release reruns its Jobs and waits for rollouts and Jobs"
    )
    ns, secret = list(yaml.safe_load_all(secret_k1["stdin"]))
    assert ns == {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": "otb-k1", "labels": {"otb.stack": "k1-alpha"}},
    }
    assert secret["metadata"] == {"name": "otb-credentials", "namespace": "otb-k1", "labels": {"otb.stack": "k1-alpha"}}
    assert set(secret["stringData"]) == {"K1_ADMIN_PASSWORD", "K1_TOKEN"}
    assert len(secret["stringData"]["K1_TOKEN"]) == 48, "a generated password, not a placeholder"
    assert set(list(yaml.safe_load_all(secret_k2["stdin"]))[1]["stringData"]) == {"K2_TOKEN"}
    assert yaml.safe_load((repo / ".otb/k8s/values/kind/k1-store.yaml").read_text()) == {
        "resources": {"requests": {"cpu": "1", "memory": "1Gi"}, "limits": {"cpu": "1", "memory": "1Gi"}}
    }
    assert harness.requests == ["/k1/k8s/ready", "/k2/k8s/ready"], "readiness polled the overridden URLs"
    assert report["runtime"] == "k8s" and report["profile"] == "kind"
    assert report["releases"] == {
        "k1-alpha": {"k1-store": {}, "k1-ingest": {}},
        "k2-beta": {"k2-store": {"jobs": ["store-setup"]}, "k2-ingest": {}},
    }
    assert {s: v["provisioned"] for s, v in report["stacks"].items()} == {"k1-alpha": [], "k2-beta": []}
    evidence["up"] = [harness.short(c) for c in calls]
    evidence["up_readiness"] = list(harness.requests)

    harness.reset()
    monkeypatch.setenv("OTB_K8S_CONTEXT", "kind-otb-w2")
    (repo / ".otb/state/k1-alpha").mkdir(parents=True)
    down(stacks, "kind", volumes=False, repo=repo)
    ctx = ("--kube-context", "kind-otb-w2")
    kctx = ("--context", "kind-otb-w2")
    manifest = "<repo>/deploy/k8s/stacks/k2-beta/store-kind.yaml"
    assert [harness.short(c) for c in harness.calls()] == [
        uninstall("k1-ingest", "otb-k1", *ctx),
        uninstall("k1-store", "otb-k1", *ctx),
        uninstall("k2-ingest", "otb-k2", *ctx),
        ["kubectl", *kctx, "delete", "--namespace", "otb-k2", "--ignore-not-found", "--wait", "-f", manifest],
    ], "releases removed in reverse order under OTB_K8S_CONTEXT; kubectl takes --context, helm --kube-context"
    assert (repo / ".otb/state/k1-alpha").exists() and not (repo / ".otb/wiped").exists()
    evidence["down"] = [harness.short(c) for c in harness.calls()]

    harness.reset()
    down(stacks, "kind", volumes=True, repo=repo)
    pvc = [harness.short(c) for c in harness.calls() if "pvc" in c]
    assert pvc == [
        ["kubectl", "--context", "kind-otb-w2", "delete", "pvc", "--all", "--namespace", "otb-k1", "--wait"],
        ["kubectl", "--context", "kind-otb-w2", "delete", "pvc", "--all", "--namespace", "otb-k2", "--wait"],
    ]
    assert not (repo / ".otb/state/k1-alpha").exists()
    assert sorted(p.name for p in (repo / ".otb/wiped").iterdir()) == ["k1-alpha", "k2-beta"]
    evidence["down_volumes"] = [harness.short(c) for c in harness.calls()]

    write_artifact(artifacts / "k8s_lifecycle.json", evidence)


def test_up_converges_from_an_interrupted_run(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, artifacts: Path
) -> None:
    """What an interrupted `up` leaves behind, and what the next `up` does with it: k1-store pending-upgrade over a
    deployed revision 1 (rolled back to 1), k1-ingest pending-install with nothing deployed (uninstalled), and a
    live k1-store Deployment on RollingUpdate while its chart now says Recreate (patched before the upgrade)."""
    repo = harness.repo
    stacks = list(registry(repo, "k8s").values())
    bootstrap(stacks, "kind", repo)
    harness.state(
        history={
            "k1-store": [{"revision": 1, "status": "deployed"}, {"revision": 2, "status": "pending-upgrade"}],
            "k1-ingest": [{"revision": 1, "status": "pending-install"}],
            "k2-ingest": [{"revision": 3, "status": "deployed"}],
        },
        deployments={"otb-k1/k1-store": {"strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1}}}},
    )
    monkeypatch.setenv("FAKE_RENDER", f"{RENDER / 'recreate'}:{RENDER / 'good'}")
    harness.reset()
    report = up(stacks, "kind", repo)
    calls = [harness.short(c) for c in harness.calls()]
    recovery = [c for c in calls if c[1] in ("rollback", "uninstall", "patch")]
    patch = json.dumps({"spec": {"strategy": {"type": "Recreate", "rollingUpdate": None}}})
    assert recovery == [
        ["helm", "rollback", "k1-store", "1", "--namespace", "otb-k1", "--wait"],
        ["kubectl", "patch", "deployment", "k1-store", "--namespace", "otb-k1", "--type", "merge", "-p", patch],
        ["helm", "uninstall", "k1-ingest", "--namespace", "otb-k1", "--wait"],
    ]
    upgrades = [c[3] for c in calls if c[:2] == ["helm", "upgrade"]]
    assert upgrades == ["k1-store", "k1-ingest", "k2-ingest"], "every release is still upgraded, in order"
    assert calls.index(recovery[0]) < calls.index(recovery[1]) < calls.index(upgrade_of(calls, "k1-store"))
    assert report["releases"]["k1-alpha"] == {
        "k1-store": {"unstuck_from": "pending-upgrade", "strategy_patched": ["k1-store"]},
        "k1-ingest": {"unstuck_from": "pending-install"},
    }
    assert report["releases"]["k2-beta"]["k2-ingest"] == {}, "a deployed release needs no recovery"

    harness.state(jobs={"otb-k2/store-setup": "Failed"})
    with pytest.raises(
        StackDefError, match=r"otb-k2/job/store-setup failed: see `kubectl -n otb-k2 logs job/store-setup`"
    ):
        up(stacks, "kind", repo)
    write_artifact(artifacts / "k8s_up_recovery.json", {"recovery": recovery, "releases": report["releases"]})


def upgrade_of(calls: list[list], release: str) -> list:
    return next(c for c in calls if c[:2] == ["helm", "upgrade"] and c[3] == release)


def test_doctor_refuses_each_failure_mode(harness: Harness, monkeypatch: pytest.MonkeyPatch, artifacts: Path) -> None:
    repo = harness.repo
    stacks = list(registry(repo, "k8s").values())
    missing = " missing or differs from charts.lock.yaml; run `otb bootstrap --runtime k8s`"
    assert doctor(stacks, ["kind"], repo) == [
        f"k1-store: {repo}/.otb/charts/store-1.0.0.tgz{missing}",
        f"k1-ingest: {repo}/.otb/charts/collector-0.1.0.tgz{missing}",
    ]
    bootstrap(stacks, "kind", repo)

    assert doctor(stacks, ["kind", "gke"], repo) == []
    gib = 2**30
    assert budgets_table(stacks, "kind", repo) == {
        "k1-alpha/backend": {"cpus": 1.0, "mem_bytes": gib},
        "k2-beta/backend": {"cpus": 1.0, "mem_bytes": gib},
        "k1-alpha/ingest": {"cpus": 0.5, "mem_bytes": 256 * 2**20},
        "k2-beta/ingest": {"cpus": 0.5, "mem_bytes": 256 * 2**20},
    }
    assert budgets_table(stacks, "gke", repo)["k1-alpha/backend"] == {"cpus": 4.0, "mem_bytes": 8 * gib}, (
        "the profile overlay, passed last, sets the rendered budget"
    )

    pod = "kind: k1-alpha/k1-store: Deployment/k1-store: "
    expected = {
        "burstable": [
            pod + "not Guaranteed (requests must equal limits for cpu and memory): ['store']",
            "kind: no backend pods for ['k1-alpha']",
        ],
        "nodigest": [pod + "container store: image 'fx/store:1.0.0' is not pinned by digest"],
        "unequal": ["kind: unequal backend budgets: k1-alpha=2.0 CPU/2.00 GiB, k2-beta=1.0 CPU/1.00 GiB"],
        "unlabelled": [pod + "labels otb.stack/otb.role missing or invalid", "kind: no backend pods for ['k1-alpha']"],
    }
    found = {}
    for case, want in expected.items():
        monkeypatch.setenv("FAKE_RENDER", f"{RENDER / case}:{RENDER / 'good'}")
        found[case] = doctor(stacks, ["kind"], repo)
        assert found[case] == want, case

    monkeypatch.setenv("FAKE_RENDER", str(RENDER / "good"))
    bg = repo / "deploy/k8s/background"
    bg.mkdir(parents=True)
    (bg / "loggen.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "otb-bg-loggen", "namespace": "otb-bg"},
                "spec": {
                    "replicas": 1,
                    "template": {
                        "metadata": {"labels": {"otb.role": "background"}},
                        "spec": {
                            "initContainers": [{"name": "build", "image": "golang:1.22", "resources": {}}],
                            "containers": [
                                {
                                    "name": "loggen",
                                    "image": "fx/loggen:1@sha256:" + "4" * 64,
                                    "resources": {
                                        "requests": {"cpu": "1", "memory": "1Gi"},
                                        "limits": {"cpu": "2", "memory": "1Gi"},
                                    },
                                }
                            ],
                        },
                    },
                },
            }
        )
    )
    found["background"] = doctor(stacks, ["kind"], repo)
    assert found["background"] == [
        "otb-bg: Deployment/otb-bg-loggen: container build: image 'golang:1.22' is not pinned by digest",
        "otb-bg: Deployment/otb-bg-loggen: not Guaranteed (requests must equal limits for cpu and memory): "
        "['loggen', 'build']",
    ]
    assert budgets_table(stacks, "kind", repo)["k1-alpha/backend"] == {"cpus": 1.0, "mem_bytes": 2**30}, (
        "the background is outside the per-stack sums"
    )
    shutil.rmtree(bg)

    wrong_profile = otb("doctor", "--runtime", "k8s", "--profile", "laptop", "--stacks", "b1")
    assert wrong_profile.returncode == 2 and "--profile laptop: the k8s runtime knows kind, gke" in wrong_profile.stderr
    write_artifact(artifacts / "k8s_doctor.json", {"good": [], "refused": found})


def real_repo_copy(tmp_path: Path) -> Path:
    """The real stacks, charts lock, profiles and values without .otb/: a checkout before `otb bootstrap`."""
    repo = tmp_path / "unbootstrapped"
    for d in ("stacks", "servers", "deploy"):
        shutil.copytree(REPO / d, repo / d, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copyfile(REPO / "uv.lock", repo / "uv.lock")
    return repo


def test_doctor_on_the_real_stacks(tmp_path: Path, artifacts: Path) -> None:
    """The integrated truth: every stack carries a k8s block (unit W1), so doctor renders them, which needs the
    charts. Before `otb bootstrap --runtime k8s` it refuses with the charts-missing line per release and nothing
    else; after it (this checkout, when its archives match the lock) it passes both profiles."""
    stacks = list(registry(REPO, "k8s").values())
    assert [s.id for s in stacks] == ["b1-lgtm", "b3-clickhouse", "b4-parseable"]
    releases = chart_releases(stacks)  # one per (chart, version): the collector chart serves all three stacks
    assert {r.chart for r in releases} == {
        "prometheus-community/prometheus",
        "grafana-community/loki",
        "grafana-community/tempo",
        "grafana-community/grafana",
        "open-telemetry/opentelemetry-collector",
        "parseable/parseable",
    }

    before = real_repo_copy(tmp_path)
    problems = doctor(stacks, ["kind", "gke"], before)
    missing = " missing or differs from charts.lock.yaml; run `otb bootstrap --runtime k8s`"
    assert problems == [f"{r.name}: {before}/.otb/charts/{chart_archive(r, before).name}{missing}" for r in releases]

    lock = chart_lock(REPO)
    bootstrapped = all(
        chart_archive(r, REPO).exists()
        and hashlib.sha256(chart_archive(r, REPO).read_bytes()).hexdigest() == locked(r, lock).sha256
        for r in releases
    ) and (shutil.which("helm") is None or post_renderer_installed())
    evidence: dict[str, object] = {"unbootstrapped_refusals": len(problems), "bootstrapped": bootstrapped}
    if bootstrapped:
        if shutil.which("helm") is None:
            pytest.skip("renders the real charts with helm")
        real = otb("doctor", "--runtime", "k8s")
        evidence["doctor"] = real.stdout.splitlines()
        assert real.returncode == 0 and real.stdout.rstrip().endswith("doctor: ok"), real.stdout + real.stderr
        assert len([ln for ln in real.stdout.splitlines() if ln.startswith(("kind", "gke"))]) == 18, (
            "backend and ingest rows for three stacks on three profiles"
        )
    write_artifact(artifacts / "k8s_doctor_real.json", evidence)


def address_fields(doc: dict) -> dict[str, str]:
    """The URL-bearing fields of one real stack.yaml, by dotted path: what the test maps to cluster addresses."""
    out = {f"ingest.{k}": doc["ingest"][k] for k in ("url", "health_url", "metrics_url")}
    out |= {f"urls.{k}": v for k, v in doc["urls"].items()}
    out |= {f"readiness.{r['name']}": r["url"] for r in doc["readiness"]}
    out |= {f"mcp.env.{k}": v for k, v in doc["mcp"].get("env", {}).items() if "://" in v}
    return out


def cluster_repo(tmp_path: Path, k8s_blocks: bool) -> tuple[Path, dict[str, str]]:
    """A copy of the real stacks with a k8s block per stack that moves every address to <stack>-<port>.svc, and
    the origin map (compose origin -> cluster origin) the gate slice is rewritten with."""
    repo = tmp_path / "cluster-repo"
    shutil.copytree(REPO / "stacks", repo / "stacks", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(REPO / "servers", repo / "servers")
    (repo / "deploy/k8s/profiles").mkdir(parents=True)
    (repo / "deploy/k8s/charts.lock.yaml").write_text("charts: {}\n")
    origin_map: dict[str, str] = {}
    for sy in sorted(repo.glob("stacks/*/stack.yaml")):
        sid = sy.parent.name
        doc = yaml.safe_load(sy.read_text())
        endpoints = {}
        for path, url in address_fields(doc).items():
            u = urlsplit(url)
            origin = f"{u.scheme}://{u.netloc}"
            origin_map[origin] = f"http://{sid}-{u.port}.otb-{sid.split('-')[0]}.svc.cluster.local:{u.port}"
            endpoints[path] = origin_map[origin] + url[len(origin) :]
        values = repo / f"deploy/k8s/stacks/{sid}/all.yaml"
        values.parent.mkdir(parents=True)
        values.write_text("{}\n")
        block = {
            "namespace": f"otb-{sid.split('-')[0]}",
            "releases": [
                {"name": sid, "chart": f"fx/{sid}", "version": "0.0.1", "values": str(values.relative_to(repo))}
            ],
            "endpoints": endpoints,
        }
        if k8s_blocks:
            sy.write_text(sy.read_text() + "\n" + yaml.safe_dump({"k8s": block}, sort_keys=False))
    return repo, origin_map


def rewrite_slice(dst: Path, origin_map: dict[str, str]) -> int:
    """A copy of the committed gate slice whose recorded URLs carry the mapped origins; returns URLs rewritten."""
    shutil.copytree(GATE_SLICE, dst)
    n = 0
    for responses in dst.glob("gate/*/" + RESPONSES):
        records = list(read_responses(responses))
        for rec in records:
            for ex in rec["exchanges"]:
                u = urlsplit(ex["url"])
                origin = f"{u.scheme}://{u.netloc}"
                if origin in origin_map:
                    ex["url"] = origin_map[origin] + ex["url"][len(origin) :]
                    n += 1
        with responses.open("wb") as f:
            for rec in records:
                append_response(f, rec)
    return n


def test_rejudge_matches_logical_addresses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, artifacts: Path) -> None:
    recorded = (GATE_SLICE / "gate/gate.json").read_bytes()
    repo, origin_map = cluster_repo(tmp_path, k8s_blocks=True)
    cluster = tmp_path / "cluster-session"
    rewritten = rewrite_slice(cluster, origin_map)
    assert rewritten == 151, "every recorded exchange of the slice names a stack origin"

    result: dict[str, object] = {"urls_rewritten": rewritten, "origins": origin_map}
    monkeypatch.setenv("OTB_RUNTIME", "k8s")
    got, _ = rejudge(cluster, repo)
    result["cluster_session_under_k8s_identical"] = got == recorded
    got_compose_fixture, _ = rejudge(GATE_SLICE, repo)
    result["compose_session_under_k8s_identical"] = got_compose_fixture == recorded
    monkeypatch.setenv("OTB_RUNTIME", "compose")
    got_compose, _ = rejudge(cluster, repo)
    result["cluster_session_under_compose_identical"] = got_compose == recorded

    stranger = tmp_path / "stranger-session"
    rewrite_slice(stranger, {o: "http://elsewhere.invalid:1" for o in origin_map})
    foreign = r"replay: recorded GET http://elsewhere\.invalid:1/api/v2/traces/"
    with pytest.raises(RuntimeError, match=foreign) as refused:
        rejudge(stranger, repo)
    result["foreign_host_refused"] = str(refused.value)

    write_artifact(artifacts / "k8s_rejudge.json", result)
    assert result["cluster_session_under_k8s_identical"], "a cluster recording rejudges under the k8s runtime"
    assert result["compose_session_under_k8s_identical"], "the committed 127.0.0.1 slice rejudges under k8s"
    assert result["cluster_session_under_compose_identical"], "and the cluster recording under compose"


def test_binary_launch_is_sha_checked_and_resolves_to_otb_home(harness: Harness, tmp_path: Path) -> None:
    repo = harness.repo
    lock = repo / "servers/fx/fx-mcp.lock.yaml"
    bin_dir = tmp_path / "bin-direct"
    assert install_binary(lock, bin_dir, "linux-amd64") == bin_dir / "fx-mcp"
    assert (bin_dir / "fx-mcp").read_bytes() == FX_MCP_SCRIPT
    with pytest.raises(StackDefError, match="no asset for windows-amd64; known: darwin-arm64, linux-amd64"):
        install_binary(lock, bin_dir, "windows-amd64")

    spec = yaml.safe_load(lock.read_text())
    spec["assets"]["linux-amd64"]["sha256"] = "0" * 64
    tampered = tmp_path / "tampered.lock.yaml"
    tampered.write_text(yaml.safe_dump(spec))
    with pytest.raises(StackDefError, match=r"linux-amd64 archive sha256 [0-9a-f]{64} differs from the lock's 0000"):
        install_binary(tampered, bin_dir, "linux-amd64")
    assert (bin_dir / "fx-mcp").read_bytes() == FX_MCP_SCRIPT, "the installed binary survives a refused download"
    assert sorted(p.name for p in bin_dir.iterdir()) == ["fx-mcp", "fx-mcp.sha256"], "no partial download left"

    b1 = registry()["b1-lgtm"]
    launch = b1.surface.mcp
    assert launch.kind == "binary" and launch.lock == "servers/grafana/mcp-grafana.lock.yaml"
    lock_sha = hashlib.sha256((REPO / launch.lock).read_bytes()).hexdigest()
    assert launch.pin == f"servers/grafana/mcp-grafana.lock.yaml@sha256:{lock_sha}"
    argv, env = resolve_stdio(launch, launch_vars(REPO) | {"B1_GRAFANA_MCP_TOKEN": "tok"}, {"HOME": "/nowhere"})
    assert argv == [
        f"{REPO}/.otb/bin/mcp-grafana", "-t", "stdio", "--enabled-tools", "datasource,prometheus,loki,tempo",
        "--disable-write", "--usage-stats", "disabled",
    ]  # fmt: skip
    assert env == {"GRAFANA_URL": "http://127.0.0.1:31300", "GRAFANA_SERVICE_ACCOUNT_TOKEN": "tok"}


@pytest.mark.skipif(
    os.environ.get("OTB_K8S_TESTS") != "1", reason="downloads the mcp-grafana release (OTB_K8S_TESTS=1)"
)
def test_b1_release_binary_answers_the_probe(tmp_path: Path, artifacts: Path) -> None:
    from otb.run.proxy import probe

    home = tmp_path / "otb-home"
    binary = install_binary(REPO / "servers/grafana/mcp-grafana.lock.yaml", home / "bin")
    version = subprocess.run([str(binary), "--version"], capture_output=True, text=True).stdout.strip()
    b1 = registry()["b1-lgtm"]
    values = launch_vars(REPO) | {"OTB_HOME": str(home), "B1_GRAFANA_MCP_TOKEN": "probe-token"}
    tools = probe(b1.surface.mcp, values, "2025-06-18")
    write_artifact(artifacts / "k8s_b1_binary_probe.json", {"version": version, "tools": tools})
    assert version == "2.1.1"
    assert set(b1.surface.phase) <= set(tools), "every tool the stack phases is listed by the release binary"
    assert {"query_prometheus", "query_loki_logs", "search_tempo_traces", "get_tempo_trace"} <= set(tools)


def test_every_k8s_endpoint_keeps_a_busy_backend_routable() -> None:
    """Every address otb uses under the k8s runtime (ingest targets, query URLs, readiness URLs, MCP hosts) is
    one of otb's own Services with publishNotReadyAddresses: true, defined in that stack's manifests with the
    URL's port. A chart's Service drops a pod whose readiness probe times out under load, and the gate then
    sees connection refused; compose never takes a slow backend out of routing."""
    from urllib.parse import urlsplit

    found: dict[str, list[str]] = {}
    for s in registry(REPO, "k8s").values():
        assert s.k8s is not None
        services: dict[str, dict] = {}
        for r in s.k8s.releases:
            if isinstance(r, ManifestRelease):
                for p in r.paths("kind", REPO):
                    for d in yaml.safe_load_all(p.read_text()):
                        if isinstance(d, dict) and d.get("kind") == "Service":
                            services[f"{d['metadata']['name']}.{d['metadata'].get('namespace', s.k8s.namespace)}"] = d
        ports_of = {k: {pt["port"] for pt in v["spec"]["ports"]} for k, v in services.items()}
        for key, value in s.k8s.endpoints.items():
            if key == "mcp.env.CLICKHOUSE_PORT":
                continue
            u = urlsplit(value if "://" in value else f"tcp://{value}")
            host, port = u.hostname or "", u.port or int(s.k8s.endpoints.get("mcp.env.CLICKHOUSE_PORT", 0))
            name = host.replace(".svc.cluster.local", "")
            assert name in services, f"{s.id}: {key} -> {host} is not an otb-owned Service"
            assert services[name]["spec"].get("publishNotReadyAddresses") is True, f"{s.id}: {key} -> {name}"
            assert port in ports_of[name], f"{s.id}: {key} port {port} not served by {name} ({ports_of[name]})"
            found.setdefault(s.id, []).append(name)
    assert {k: sorted(set(v)) for k, v in found.items()} == {
        "b1-lgtm": [
            "otb-q-grafana.otb-b1",
            "otb-q-ingest-b1.otb-b1",
            "otb-q-loki.otb-b1",
            "otb-q-prometheus.otb-b1",
            "otb-q-tempo.otb-b1",
        ],
        "b3-clickhouse": ["otb-q-clickhouse.otb-b3", "otb-q-ingest-b3.otb-b3"],
        "b4-parseable": ["otb-q-ingest-b4.otb-b4", "otb-q-parseable.otb-b4"],
    }
