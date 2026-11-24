"""Live: one demo instance on a Kubernetes cluster (deploy/k8s/demo/install.sh k1 <profile>), captured from this host
with `otb capture scenarios/k8s-smoke.yaml --decoy-driver k8s` while the demo collector exports to this host's
gateway. Runs only with OTB_K8S_TESTS=1 and OTB_K8S_CONTEXT set (a local kind cluster; never a shared one), takes
about 15 minutes, and deletes the instance's namespace before and after.

    OTB_K8S_TESTS=1 OTB_K8S_CONTEXT=kind-otb-w3 uv run pytest -q tests/k8s/test_demo_capture.py

The demo collector reaches the gateway at OTB_K8S_GATEWAY_HOST (default host.docker.internal, which OrbStack and
Docker Desktop resolve inside kind's node and so inside its pods); flagd-ui, OFREP and Locust are port-forwarded.

Ways it could fail, each asserted below:
- the rendered chart has a pod that is not Guaranteed or an image not pinned by digest (tests/k8s/demo_check.py);
- the collector cannot reach the gateway, so the corpus stays empty, or seals without one of the three signals;
- a record reaches the corpus without cloud.region = service.instance.id = the instance;
- the flag write is not confirmed by readback and by flagd itself (OFREP), or the decoys fail;
- kubeletstats metrics are missing, or carry another namespace's pods;
- the rollout restart of cart is not visible in the data: no second cart pod in the kubeletstats series, no new
  cumulative series start for cart inside the decoy's window.

Evidence: tests/k8s/artifacts/demo_capture.json (OTB_WRITE_ARTIFACTS=1), and the corpus in .otb/k8s/demo-capture.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import orjson
import pytest
import yaml

from otb.archive import Archive
from otb.scenario import RealisedSchedule

REPO = Path(__file__).resolve().parents[2]
CONTEXT = os.environ.get("OTB_K8S_CONTEXT", "")
PROFILE = os.environ.get("OTB_K8S_DEMO_PROFILE", "kind")
GATEWAY_HOST = os.environ.get("OTB_K8S_GATEWAY_HOST", "host.docker.internal")
INSTANCE = "k1"
NAMESPACE = f"otb-demo-{INSTANCE}"
SCENARIO = REPO / "scenarios" / "k8s-smoke.yaml"
WRITE = os.environ.get("OTB_WRITE_ARTIFACTS") == "1"
NS = 1_000_000_000

pytestmark = pytest.mark.skipif(
    os.environ.get("OTB_K8S_TESTS") != "1" or not CONTEXT, reason="needs OTB_K8S_TESTS=1 and OTB_K8S_CONTEXT"
)
sys.path.insert(0, str(Path(__file__).parent))
import demo_check  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def kubectl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["kubectl", "--context", CONTEXT, *args], capture_output=True, text=True, check=check)


def delete_instance() -> None:
    subprocess.run(["helm", "--kube-context", CONTEXT, "uninstall", NAMESPACE, "-n", NAMESPACE, "--wait"], check=False)
    kubectl("delete", "namespace", NAMESPACE, "--ignore-not-found", "--wait=true", "--timeout=180s", check=False)


def attrs(kvs: list[dict] | None) -> dict[str, object]:
    out = {}
    for kv in kvs or []:
        v = kv.get("value") or {}
        out[kv["key"]] = next(iter(v.values()), None) if v else None
    return out


def by_start(item: tuple[str, list[int]]) -> int:
    return item[1][0]


def summarise(corpus: Path, window: tuple[int, int]) -> dict:
    """What the corpus holds, per signal and for the kubeletstats metrics, without a backend."""
    a = Archive.open(corpus, verify="index")
    regions: dict[str, Counter] = defaultdict(Counter)
    kubelet_metrics: dict[str, set[str]] = defaultdict(set)
    kubelet_resource_keys: set[str] = set()
    kubelet_namespaces: Counter = Counter()
    cart_pods: dict[str, list[int]] = {}
    cart_series_starts: set[int] = set()
    services: Counter = Counter()
    keys = {"traces": ("resourceSpans", "scopeSpans", "spans"), "logs": ("resourceLogs", "scopeLogs", "logRecords")}
    for seg in a.segments():
        for _, body in a.bodies(seg):
            doc = orjson.loads(body)
            if seg.signal in keys:
                rk, sk, ik = keys[seg.signal]
                for r in doc.get(rk, []):
                    ra = attrs(r.get("resource", {}).get("attributes"))
                    n = sum(len(s.get(ik, [])) for s in r.get(sk, []))
                    ok = ra.get("cloud.region") == INSTANCE and ra.get("service.instance.id") == INSTANCE
                    regions[seg.signal]["labelled" if ok else f"other:{ra.get('cloud.region')}"] += n
                    services[ra.get("service.name")] += n
                continue
            for r in doc.get("resourceMetrics", []):
                ra = attrs(r.get("resource", {}).get("attributes"))
                ok = ra.get("cloud.region") == INSTANCE and ra.get("service.instance.id") == INSTANCE
                for s in r.get("scopeMetrics", []):
                    kubelet = "kubeletstats" in (s.get("scope") or {}).get("name", "")
                    for m in s.get("metrics", []):
                        data = next((v for k, v in m.items() if isinstance(v, dict) and "dataPoints" in v), {})
                        points = data.get("dataPoints", [])
                        regions["metrics"]["labelled" if ok else f"other:{ra.get('cloud.region')}"] += len(points)
                        if kubelet:
                            kubelet_resource_keys.update(ra)
                            kubelet_namespaces[ra.get("k8s.namespace.name")] += len(points)
                            for p in points:
                                kubelet_metrics[m["name"]].update(attrs(p.get("attributes")))
                            if ra.get("k8s.container.name") == "cart":
                                ts = [int(p["timeUnixNano"]) for p in points]
                                span = cart_pods.setdefault(ra["k8s.pod.name"], [min(ts), max(ts)])
                                span[:] = [min(span[0], *ts), max(span[1], *ts)]
                        elif ra.get("service.name") == "cart" and data.get("isMonotonic") is not None:
                            for p in points:
                                start = int(p.get("startTimeUnixNano") or 0)
                                if window[0] <= start < window[1]:
                                    cart_series_starts.add(start)
    return {
        "records": {s: dict(sorted(c.items())) for s, c in sorted(regions.items())},
        "services": sorted(str(s) for s in services),
        "kubeletstats": {
            "metrics": {k: sorted(v) for k, v in sorted(kubelet_metrics.items())},
            "resource_attributes": sorted(kubelet_resource_keys),
            "namespaces": dict(kubelet_namespaces),
        },
        "cart_pods": {p: [(t - window[0]) / NS for t in span] for p, span in sorted(cart_pods.items(), key=by_start)},
        "cart_series_starts_in_window": len(cart_series_starts),
    }


def event_line(e: dict) -> str:
    line = f"{e['slot_id']} {e['action']} {e['phase']} {'ok' if e['ok'] else 'FAILED'}"
    if e["action"] == "flag.confirm":
        line += f" readback={e['detail'].get('readback')} served={e['detail'].get('served')}"
    return line


def test_demo_on_kubernetes_feeds_a_sealed_corpus(tmp_path: Path) -> None:
    rendered = subprocess.run(
        [str(REPO / "deploy/k8s/demo/install.sh"), "--template", INSTANCE, PROFILE],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    docs = [d for d in yaml.safe_load_all(rendered) if d]
    compose = json.loads(demo_check.COMPOSE.read_text())
    source = yaml.safe_load(demo_check.SOURCE.read_text())
    assert demo_check.check(docs, INSTANCE, PROFILE, compose, source) == []

    out = REPO / ".otb" / "k8s" / "demo-capture"
    shutil.rmtree(out, ignore_errors=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    delete_instance()
    port = free_port()
    pf_ports = {"flagd-ui": free_port(), "ofrep": free_port(), "locust": free_port()}
    log = tmp_path / "capture.log"
    argv = [sys.executable, "-m", "otb.cli", "capture", str(SCENARIO), "--out", str(out), "--decoy-driver", "k8s"]
    argv += ["--kube-context", CONTEXT, "--listen", f"0.0.0.0:{port}", "--wait-demo", "900", "--drain", "30"]
    argv += ["--flagd-ui", f"http://127.0.0.1:{pf_ports['flagd-ui']}"]
    argv += ["--ofrep", f"http://127.0.0.1:{pf_ports['ofrep']}"]
    argv += ["--locust", f"http://127.0.0.1:{pf_ports['locust']}"]
    forwards: list[subprocess.Popen] = []
    with log.open("ab") as f:
        cap = subprocess.Popen(argv, cwd=REPO, stdout=f, stderr=subprocess.STDOUT)
    try:
        while "gateway on" not in log.read_text():
            assert cap.poll() is None, log.read_text()
            time.sleep(0.5)
        env = os.environ | {"OTB_K8S_CONTEXT": CONTEXT, "OTB_GATEWAY": f"http://{GATEWAY_HOST}:{port}"}
        subprocess.run([str(REPO / "deploy/k8s/demo/install.sh"), INSTANCE, PROFILE], env=env, check=True)
        for svc, local, remote in (
            ("flagd", pf_ports["flagd-ui"], 4000),
            ("flagd", pf_ports["ofrep"], 8016),
            ("load-generator", pf_ports["locust"], 8089),
        ):
            pf = ["kubectl", "--context", CONTEXT, "-n", NAMESPACE, "port-forward", f"svc/{svc}", f"{local}:{remote}"]
            forwards.append(subprocess.Popen(pf, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        source_yaml = subprocess.run(
            [sys.executable, str(REPO / "deploy/k8s/demo/source.py"), INSTANCE],
            env=os.environ | {"OTB_K8S_CONTEXT": CONTEXT},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        exit_code = cap.wait(timeout=1500)
    finally:
        for pf in forwards:
            pf.kill()
        if cap.poll() is None:
            cap.terminate()
            cap.wait(timeout=60)
    verify = subprocess.run(
        [sys.executable, "-m", "otb.cli", "corpus", "verify", str(out), "--level", "full"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    events = [json.loads(line) for line in (out / "events.jsonl").read_text().splitlines()]
    sched = RealisedSchedule.from_yaml((out / "schedule.yaml").read_bytes())
    entries = {e.slot_id: e for e in sched.entries}
    d1 = entries["d1"].window
    data = summarise(out, (d1.start, d1.end))
    delete_instance()

    t0 = orjson.loads((out / "capture.json").read_bytes())["capture_start_ns"]
    result = {
        "context": CONTEXT,
        "profile": PROFILE,
        "exit_code": exit_code,
        "verify": verify.stdout.strip().splitlines()[-1] if verify.stdout.strip() else verify.stderr[-300:],
        "sealed": Archive.open(out, verify="index").manifest.sealed,
        "status": {s: e.status for s, e in sorted(entries.items())},
        "windows_s": {s: [round((w - t0) / NS, 1) for w in (e.window.start, e.window.end)] for s, e in entries.items()},
        "events": [event_line(e) for e in events],
        "source": yaml.safe_load(source_yaml),
        **data,
    }
    text = json.dumps(result, indent=1, default=sorted) + "\n"
    (tmp_path / "demo_capture.json").write_text(text)
    if WRITE:
        (REPO / "tests/k8s/artifacts").mkdir(exist_ok=True)
        (REPO / "tests/k8s/artifacts/demo_capture.json").write_text(text)
    print(text)

    assert exit_code == 0, log.read_text()[-3000:]
    assert result["sealed"] and "ok (0 problems)" in result["verify"], result["verify"]
    assert result["status"] == {"d1": "realised", "d2": "realised", "f1": "realised"}, result["events"]
    confirms = [e for e in events if e["slot_id"] == "f1" and e["action"] == "flag.confirm"]
    served = [(e["phase"], e["ok"], e["detail"]["served"]) for e in confirms]
    assert served == [("on", True, "50%"), ("off", True, "off")], served
    for signal in ("traces", "logs", "metrics"):
        assert set(result["records"][signal]) == {"labelled"} and result["records"][signal]["labelled"] > 0, result
    assert set(data["kubeletstats"]["namespaces"]) == {NAMESPACE}, data["kubeletstats"]
    assert {"container.cpu.usage", "container.memory.working_set"} <= set(data["kubeletstats"]["metrics"])
    assert len(data["cart_pods"]) == 2, data["cart_pods"]
    first, second = data["cart_pods"].values()
    assert first[1] < (d1.end - d1.start) / NS and 0 <= second[0] < (d1.end - d1.start) / NS, data["cart_pods"]
    assert data["cart_series_starts_in_window"] > 0
