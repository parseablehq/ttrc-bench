"""`otb capture --decoy-driver k8s` with two demo instances, a and b, each with a stand-in flagd-ui and a stand-in
Locust, and a stub `kubectl` on PATH that logs every call. No cluster.

Plan (offsets from capture start):
  d1 a 1-2 s  benign_restart of cart in otb-demo-a; its rollout finishes.
  d2 b 2-3 s  benign_restart of cart in demo-b-ns; its rollout never finishes (the stub's `rollout status` fails).
  d3 a 4-6 s  traffic_gap: Locust a stops, then goes back to 7 users.
  f1 a 7-8 s  paymentFailure 50%.
The planner keeps unrelated slots apart in time across instances, so no two overlap.

Ways it could fail, each asserted below:
- a restart runs in another instance's namespace, or under kubectl's current context instead of --kube-context;
- a restart whose rollout never finishes is still realised in the schedule;
- the restart's window opens only after the rollout finished, after the new pod's first telemetry;
- a traffic gap goes to another instance's Locust, or is not undone;
- a scenario that restarts flagd (which reverts every injected flag), or a Deployment the namespace does not have,
  passes preflight and the capture starts;
- a scenario with a background is captured without its generators, they go up after the capture start (their
  first batches would find no gateway), their rate knobs are not the scenario's, they are left running after
  the seal, or --no-background still applies them.

Evidence: tests/e2e/artifacts/capture_k8s_decoys.json.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import orjson
from conftest import free_port, write_artifact

from otb.scenario import RealisedSchedule

REPO = Path(__file__).resolve().parents[2]
NS = 1_000_000_000
SCENARIO = """id: capture-k8s-decoys
seed: 3
duration_s: 10
instances: [a, b]
min_gap_s: 0
slots:
  - { id: d1, kind: decoy, type: benign_restart, target_in: [cart], start_s: { lo: 1, hi: 1 }, duration_s: { lo: 1, hi: 1 }, instance: a }
  - { id: d2, kind: decoy, type: benign_restart, target_in: [cart], start_s: { lo: 2, hi: 2 }, duration_s: { lo: 1, hi: 1 }, instance: b }
  - { id: d3, kind: decoy, type: traffic_gap, target_in: [load-generator], params: { users_after: "7" }, start_s: { lo: 4, hi: 4 }, duration_s: { lo: 2, hi: 2 }, instance: a }
  - { id: f1, kind: fault, flag_in: [paymentFailure], variant_in: ["50%"], start_s: { lo: 7, hi: 7 }, duration_s: { lo: 1, hi: 1 }, instance: a }
"""  # noqa: E501
REFUSED = """id: capture-k8s-refused
seed: 3
duration_s: 5
instances: [a]
min_gap_s: 0
slots:
  - { id: d1, kind: decoy, type: benign_restart, target_in: [flagd], start_s: { lo: 1, hi: 1 }, duration_s: { lo: 1, hi: 1 }, instance: a }
  - { id: d2, kind: decoy, type: benign_restart, target_in: [nosuch], start_s: { lo: 3, hi: 3 }, duration_s: { lo: 1, hi: 1 }, instance: a }
"""  # noqa: E501
STUB = """#!/bin/sh
echo "$*" >> {log}
case "$*" in
  *"apply -f -"*) cat > {log}.apply;;
  *"delete --ignore-not-found --wait -f -"*) cat > {log}.delete;;
  *"get deployment/nosuch"*) echo 'Error from server (NotFound): deployments.apps "nosuch" not found' >&2; exit 1;;
  *"rollout status"*"-n demo-b-ns"*) echo 'error: timed out waiting for the condition' >&2; exit 1;;
  *"rollout restart deployment/"*) echo "deployment.apps/cart restarted";;
esac
exit 0
"""


class Locust:
    """Records each request's path and form body and answers 200, as Locust's /swarm and /stop do."""

    def __init__(self) -> None:
        self.calls: list[tuple[float, str, str]] = []
        calls = self.calls

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: object) -> None:
                pass

            def _reply(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                calls.append((time.time(), self.path, body))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            do_GET = do_POST = _reply

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def start_flagd(tmp_path: Path, name: str) -> tuple[subprocess.Popen, str]:
    port = free_port()
    flag_file = tmp_path / f"{name}.flagd.json"
    shutil.copy(REPO / "tests/fixtures/flagd/demo.flagd.json", flag_file)
    argv = [sys.executable, str(REPO / "tests/fixtures/fake_flagd_ui.py"), str(port), str(flag_file)]
    return subprocess.Popen([*argv, str(tmp_path / f"{name}.writes.jsonl")]), f"http://127.0.0.1:{port}"


def capture(tmp_path: Path, scenario: str, out: Path, extra: list[str]) -> tuple[subprocess.CompletedProcess, Path]:
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir(exist_ok=True)
    log = tmp_path / "kubectl.log"
    (stub_bin / "kubectl").write_text(STUB.format(log=log))
    (stub_bin / "kubectl").chmod(0o755)
    (tmp_path / f"{out.name}.yaml").write_text(scenario)
    argv = [sys.executable, "-m", "otb.cli", "capture", str(tmp_path / f"{out.name}.yaml"), "--out", str(out)]
    argv += ["--decoy-driver", "k8s", "--kube-context", "kind-otb-test", "--flagd-retry", "4", "--wait-demo", "10"]
    argv += ["--listen", f"127.0.0.1:{free_port()}", "--drain", "1", "--stale-s", "inf", *extra]
    env = os.environ | {"PATH": f"{stub_bin}{os.pathsep}{os.environ['PATH']}"}
    env.pop("OTB_K8S_CONTEXT", None)
    p = subprocess.run(argv, cwd=REPO, capture_output=True, text=True, env=env, timeout=120)
    return p, log


def test_k8s_decoys_restart_in_the_instance_namespace(tmp_path: Path, artifacts: Path) -> None:
    flagd = {i: start_flagd(tmp_path, i) for i in ("a", "b")}
    locust = {i: Locust() for i in ("a", "b")}
    out = tmp_path / "corpus"
    try:
        p, log = capture(
            tmp_path,
            SCENARIO,
            out,
            [
                "--flagd-ui", ",".join(flagd[i][1] for i in ("a", "b")),
                "--locust", ",".join(locust[i].url for i in ("a", "b")),
                "--k8s-namespace", "otb-demo-a,demo-b-ns",
            ],
        )  # fmt: skip
    finally:
        for proc, _ in flagd.values():
            proc.kill()
        for lc in locust.values():
            lc.server.shutdown()
    assert p.returncode == 0, p.stdout + p.stderr

    sched = RealisedSchedule.from_yaml((out / "schedule.yaml").read_bytes())
    entries = {e.slot_id: e for e in sched.entries}
    t0 = orjson.loads((out / "capture.json").read_bytes())["capture_start_ns"]
    events = [json.loads(line) for line in (out / "events.jsonl").read_text().splitlines()]
    restart_on = next(e for e in events if e["slot_id"] == "d1" and e["phase"] == "on")
    calls = log.read_text().splitlines()
    result = {
        "exit_code": p.returncode,
        "status": {s: e.status for s, e in sorted(entries.items())},
        # preflight runs in instance order; then each instance's timeline runs on its own thread
        "kubectl_calls": calls[:2] + sorted(calls[2:]),
        "d1_on_detail": {k: restart_on["detail"][k] for k in ("argv", "stdout")},
        "d2_off_stderr": next(x.detail["stderr"].strip() for x in entries["d2"].actions if x.detail["phase"] == "off"),
        "locust_a": [(path, body) for _, path, body in locust["a"].calls],
        "locust_b": [(path, body) for _, path, body in locust["b"].calls],
    }
    windows = {s: [round((w - t0) / NS, 1) for w in (e.window.start, e.window.end)] for s, e in entries.items()}
    write_artifact(artifacts / "capture_k8s_decoys.json", result, timing={"window_offsets_s": windows})

    assert abs(windows["d1"][0] - 1) < 0.5, f"d1's window opened after its restart was accepted: {windows['d1']}"
    assert result == {
        "exit_code": 0,
        "status": {"d1": "realised", "d2": "failed", "d3": "realised", "f1": "realised"},
        "kubectl_calls": [
            "--context kind-otb-test get deployment/cart -n otb-demo-a -o name",
            "--context kind-otb-test get deployment/cart -n demo-b-ns -o name",
            "--context kind-otb-test rollout restart deployment/cart -n demo-b-ns",
            "--context kind-otb-test rollout restart deployment/cart -n otb-demo-a",
            "--context kind-otb-test rollout status deployment/cart -n demo-b-ns --timeout=180s",
            "--context kind-otb-test rollout status deployment/cart -n otb-demo-a --timeout=180s",
        ],
        "d1_on_detail": {
            "argv": "kubectl --context kind-otb-test rollout restart deployment/cart -n otb-demo-a",
            "stdout": "deployment.apps/cart restarted\n",
        },
        "d2_off_stderr": "error: timed out waiting for the condition",
        "locust_a": [("/stop", ""), ("/swarm", "user_count=7&spawn_rate=7")],
        "locust_b": [],
    }


def test_k8s_preflight_refuses_flagd_and_missing_deployments(tmp_path: Path) -> None:
    proc, url = start_flagd(tmp_path, "a")
    try:
        p, log = capture(tmp_path, REFUSED, tmp_path / "refused", ["--flagd-ui", url, "--locust", "http://127.0.0.1:9"])
    finally:
        proc.kill()
    assert p.returncode == 1, p.stdout + p.stderr
    assert "d1: restarting flagd would revert the injected flags" in p.stderr, p.stderr
    assert 'd2: a: Error from server (NotFound): deployments.apps "nosuch" not found' in p.stderr, p.stderr
    assert log.read_text().splitlines() == ["--context kind-otb-test get deployment/nosuch -n otb-demo-a -o name"]
    assert not (tmp_path / "refused" / "events.jsonl").exists()


BACKGROUND = """id: capture-k8s-bg
runtime: k8s
seed: 3
duration_s: 4
instances: [a]
min_gap_s: 0
background: { mb_per_s: 0.4, logs_share: 0.6 }
slots:
  - { id: d1, kind: decoy, type: benign_restart, target_in: [cart], start_s: { lo: 1, hi: 1 }, duration_s: { lo: 1, hi: 1 }, instance: a }
"""  # noqa: E501


def test_k8s_capture_applies_the_background_before_the_start_and_removes_it_after(
    tmp_path: Path, artifacts: Path
) -> None:
    import yaml

    flagd, flagd_url = start_flagd(tmp_path, "a")
    locust = Locust()
    try:
        out = tmp_path / "bg"
        p, log = capture(tmp_path, BACKGROUND, out, ["--flagd-ui", flagd_url, "--locust", locust.url])
        assert p.returncode == 0, p.stdout + p.stderr
        calls = log.read_text().splitlines()
        applied = [d for d in yaml.safe_load_all((tmp_path / "kubectl.log.apply").read_text()) if d]
        deleted = [d for d in yaml.safe_load_all((tmp_path / "kubectl.log.delete").read_text()) if d]
        knobs = {
            d["metadata"]["name"]: d["data"]
            for d in applied
            if d["kind"] == "ConfigMap" and d["metadata"]["name"] in ("otb-bg-loggen", "otb-bg-loadgen")
        }
        result = {
            "order": [c.split()[2] if c.startswith("--context") else c.split()[0] for c in calls],
            "applied_kinds": sorted(f"{d['kind']}/{d['metadata']['name']}" for d in applied),
            "deleted_kinds": sorted(f"{d['kind']}/{d['metadata']['name']}" for d in deleted),
            "logs_per_min": knobs["otb-bg-loggen"]["LOGS_PER_MIN"],
            "interval_ms": knobs["otb-bg-loadgen"]["INTERVAL_MS"],
            "stdout": [ln for ln in p.stdout.splitlines() if "background" in ln],
        }
        write_artifact(artifacts / "capture_k8s_background.json", result)
        assert result["order"][:4] == ["apply", "rollout", "rollout", "rollout"], "the background goes up first"
        assert result["order"].index("get") > 3, "then preflight"
        assert result["order"][-1] == "delete" and result["order"].count("delete") == 1
        assert result["logs_per_min"] == "13649" and result["interval_ms"] == "4712", "0.4 MB/s, 60% logs"
        kept = {k for k in result["applied_kinds"] if k.startswith(("Namespace/", "PersistentVolumeClaim/"))}
        assert kept == {
            "Namespace/otb-bg",
            "PersistentVolumeClaim/otb-bg-loadgen-cache",
            "PersistentVolumeClaim/otb-bg-loggen-cache",
        }
        assert set(result["deleted_kinds"]) == set(result["applied_kinds"]) - kept, "the namespace and the caches stay"
        assert result["stdout"] == [
            "otb capture: background up in otb-bg: otb-bg-collector, otb-bg-loadgen, otb-bg-loggen",
            "otb capture: background removed from otb-bg",
        ]

        (tmp_path / "kubectl.log").unlink()
        p, log = capture(
            tmp_path,
            BACKGROUND,
            tmp_path / "nobg",
            ["--flagd-ui", flagd_url, "--locust", locust.url, "--no-background"],
        )
        assert p.returncode == 0, p.stdout + p.stderr
        assert not any(c.split()[2] == "apply" for c in log.read_text().splitlines())
    finally:
        flagd.terminate()
        locust.server.shutdown()
