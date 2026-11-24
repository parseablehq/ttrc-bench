"""`otb capture --decoy-driver k8s` with releases: bad-deploy faults (a fault slot with `relation: deploy` and
params.version_to, its flag intlShippingSlowdown) and a benign_deploy decoy. Stand-in flagd-ui per instance, and
tests/fixtures/fake_kubectl_deploy.py on PATH holding the demo's Deployments. No cluster. A bad deploy sets
service.version 3.0.1 in shipping's OTEL_RESOURCE_ATTRIBUTES, waits for the rollout, then turns its flag on; the
rollback turns the flag off, restores the recorded original value and waits for that rollout. A benign_deploy does
the same with no flag.

Ways it could fail, each asserted below:
- the change runs in another instance's namespace, without `-n` (kubectl's current namespace), or under kubectl's
  current context instead of --kube-context;
- it changes a Deployment that is not the demo's (one without the label app.kubernetes.io/part-of=opentelemetry-demo,
  such as a same-named Deployment in kube-system), and the capture starts;
- the telemetry would not show the release: service.version unchanged, the other resource attributes
  (service.namespace, service.criticality) lost, or another container changed;
- the flag goes on before the release's rollout finished, so old-version pods serve slow requests, or stays on
  after the rollback starts, so the restored version serves them;
- the original value is not recorded before the change, so a crash after the change leaves nothing to restore, or
  a resumed release reads the changed value as the original and the rollback "restores" the bad release;
- a crash between on and off: the resumed capture re-releases, skips the rollback, or does not realise the window;
- a crash during the release, restarted after its slot ended: the skipped release is never rolled back;
- a release whose rollout never becomes ready is realised, turns the flag on, or stays on the cluster until the
  slot ends;
- an earlier release whose rollback never gets ready is retried over a later release of the same Deployment, and
  turns that release's flag off or puts back the version it replaced;
- SIGTERM while a release is out, with flagd slow to serve the flag's off: the capture exits leaving the Deployment
  on the release;
- the schedule does not say what was deployed: no class bad_deploy, root service, relation deploy or target, no
  from and to version, no rollback time; or the realised window is not [release confirmed, rollback confirmed);
- a benign_deploy changes a flag, is not rolled back, or is realised before its rollback is ready;
- a deploy fault under the Compose driver, which has no way to deploy, starts the capture; a deploy slot without
  params.version_to plans.

Evidence: tests/e2e/artifacts/capture_k8s_deploy.json, capture_k8s_deploy_resume.json.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from conftest import FIXTURES, free_port, write_artifact

from otb.scenario import RealisedSchedule

REPO = Path(__file__).resolve().parents[2]
NS = 1_000_000_000
ORIGINAL = "service.namespace=opentelemetry-demo,service.version=3.0.0,service.criticality=high"
RELEASED = "service.namespace=opentelemetry-demo,service.version=3.0.1,service.criticality=high"
SCENARIO = """id: capture-k8s-deploy
seed: 3
duration_s: 9
instances: [a, b]
min_gap_s: 0
slots:
  - { id: f1, kind: fault, flag_in: [intlShippingSlowdown], relation: deploy, params: { version_to: 3.0.1 }, variant_in: ["5sec"], start_s: { lo: 1, hi: 1 }, duration_s: { lo: 2, hi: 2 }, instance: a }
  - { id: f2, kind: fault, flag_in: [intlShippingSlowdown], relation: deploy, params: { version_to: 3.0.1 }, variant_in: ["5sec"], start_s: { lo: 4, hi: 4 }, duration_s: { lo: 2, hi: 2 }, instance: b }
  - { id: d1, kind: decoy, type: benign_deploy, target_in: [quote], params: { version_to: 3.0.1 }, start_s: { lo: 6, hi: 6 }, duration_s: { lo: 2, hi: 2 }, instance: a }
"""  # noqa: E501
RESUME = """id: capture-k8s-deploy-resume
seed: 3
duration_s: 20
instances: [a]
min_gap_s: 0
slots:
  - { id: f1, kind: fault, flag_in: [intlShippingSlowdown], relation: deploy, params: { version_to: 3.0.1 }, variant_in: ["10sec"], start_s: { lo: 2, hi: 2 }, duration_s: { lo: 14, hi: 14 }, instance: a }
"""  # noqa: E501
LATE = """id: capture-k8s-deploy-late
seed: 3
duration_s: 12
instances: [a]
min_gap_s: 0
slots:
  - { id: f1, kind: fault, flag_in: [intlShippingSlowdown], relation: deploy, params: { version_to: 3.0.1 }, variant_in: ["5sec"], start_s: { lo: 2, hi: 2 }, duration_s: { lo: 3, hi: 3 }, instance: a }
"""  # noqa: E501
STOPPED = """id: capture-k8s-deploy-stopped
seed: 3
duration_s: 60
instances: [a]
min_gap_s: 0
slots:
  - { id: f1, kind: fault, flag_in: [intlShippingSlowdown], relation: deploy, params: { version_to: 3.0.1 }, variant_in: ["5sec"], start_s: { lo: 1, hi: 1 }, duration_s: { lo: 50, hi: 50 }, instance: a }
"""  # noqa: E501
SUPERSEDED = """id: capture-k8s-deploy-superseded
seed: 3
duration_s: 10
instances: [a]
min_gap_s: 0
slots:
  - { id: s1, kind: fault, flag_in: [intlShippingSlowdown], relation: deploy, params: { version_to: 3.0.1 }, variant_in: ["5sec"], start_s: { lo: 1, hi: 1 }, duration_s: { lo: 2, hi: 2 }, instance: a }
  - { id: s2, kind: fault, flag_in: [intlShippingSlowdown], relation: deploy, params: { version_to: 3.0.2 }, variant_in: ["5sec"], start_s: { lo: 5, hi: 5 }, duration_s: { lo: 3, hi: 3 }, instance: a }
"""  # noqa: E501
UNVERSIONED = """id: capture-k8s-deploy-unversioned
seed: 3
duration_s: 5
instances: [a]
slots:
  - { id: f1, kind: fault, flag_in: [intlShippingSlowdown], relation: deploy, start_s: { lo: 1, hi: 1 }, duration_s: { lo: 1, hi: 1 }, instance: a }
"""  # noqa: E501
REFUSED = """id: capture-k8s-deploy-refused
seed: 3
duration_s: 5
instances: [a]
min_gap_s: 0
slots:
  - { id: f1, kind: fault, flag_in: [intlShippingSlowdown], relation: deploy, params: { version_to: 3.0.1 }, start_s: { lo: 1, hi: 1 }, duration_s: { lo: 1, hi: 1 }, instance: a }
"""  # noqa: E501


def deployment(ns: str, labels: dict[str, str] | None = None, name: str = "shipping") -> dict:
    env = [{"name": "FLAGD_HOST", "value": "flagd"}, {"name": "OTEL_RESOURCE_ATTRIBUTES", "value": ORIGINAL}]
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": ns,
            "labels": labels or {"app.kubernetes.io/part-of": "opentelemetry-demo", "app.kubernetes.io/name": name},
        },
        "spec": {"template": {"spec": {"containers": [{"name": name, "env": env}]}}},
    }


def cluster(tmp_path: Path, state: dict) -> tuple[Path, Path]:
    """The fake kubectl on PATH, its state file and its call log."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "kubectl"
    stub.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FIXTURES / "fake_kubectl_deploy.py"}" "$@"\n')
    stub.chmod(0o755)
    path = tmp_path / "cluster.json"
    path.write_text(json.dumps(state))
    return path, path.with_suffix(".log")


def start_flagd(tmp_path: Path, name: str) -> tuple[subprocess.Popen, str, Path]:
    port = free_port()
    flag_file = tmp_path / f"{name}.flagd.json"
    shutil.copy(REPO / "tests/fixtures/flagd/demo.flagd.json", flag_file)
    writes = tmp_path / f"{name}.writes.jsonl"
    argv = [sys.executable, str(REPO / "tests/fixtures/fake_flagd_ui.py"), str(port), str(flag_file), str(writes)]
    return subprocess.Popen(argv), f"http://127.0.0.1:{port}", writes


def capture_argv(tmp_path: Path, scenario: str, out: Path, extra: list[str], driver: str = "k8s") -> list[str]:
    (tmp_path / f"{out.name}.yaml").write_text(scenario)
    argv = [sys.executable, "-m", "otb.cli", "capture", str(tmp_path / f"{out.name}.yaml"), "--out", str(out)]
    argv += ["--decoy-driver", driver, "--flagd-retry", "4", "--wait-demo", "10"]
    if driver == "k8s":
        argv += ["--kube-context", "kind-otb-test"]
    return [*argv, "--listen", f"127.0.0.1:{free_port()}", "--drain", "1", "--stale-s", "inf", *extra]


def get(ns: str, d: str = "shipping") -> str:
    return f"--context kind-otb-test get deployment/{d} -n {ns} -o json"


def set_env(ns: str, value: str, d: str = "shipping") -> str:
    return f"--context kind-otb-test set env deployment/{d} -n {ns} -c {d} OTEL_RESOURCE_ATTRIBUTES={value}"


def rollout(ns: str, d: str = "shipping") -> str:
    return f"--context kind-otb-test rollout status deployment/{d} -n {ns} --timeout=180s"


def capture_env(tmp_path: Path, state_path: Path) -> dict[str, str]:
    env = os.environ | {"PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}"}
    env["FAKE_KUBECTL_STATE"] = str(state_path)
    env.pop("OTB_K8S_CONTEXT", None)
    return env


def calls(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def flag_writes(writes: Path) -> list[str]:
    return [json.loads(line)["variants"]["intlShippingSlowdown"] for line in writes.read_text().splitlines()]


def resource_env(state_path: Path, key: str) -> str:
    """The OTEL_RESOURCE_ATTRIBUTES the fake cluster holds for "<namespace>/<deployment>"."""
    d = json.loads(state_path.read_text())["deployments"][key]
    return next(
        e["value"] for e in d["spec"]["template"]["spec"]["containers"][0]["env"] if e["name"].startswith("OTEL")
    )


def steps(entry) -> list[list]:
    return [[a.action, a.detail["phase"], a.ok] for a in entry.actions]


def test_k8s_bad_deploy_releases_then_rolls_back(tmp_path: Path, artifacts: Path) -> None:
    state_path, log = cluster(
        tmp_path,
        {
            "deployments": {
                "otb-demo-a/shipping": deployment("otb-demo-a"),
                "otb-demo-a/quote": deployment("otb-demo-a", name="quote"),
                "demo-b-ns/shipping": deployment("demo-b-ns"),
            },
            "never_ready": {"demo-b-ns": "3.0.1"},
        },
    )
    flagd = {i: start_flagd(tmp_path, i) for i in ("a", "b")}
    out = tmp_path / "corpus"
    extra = [
        "--flagd-ui",
        ",".join(flagd[i][1] for i in ("a", "b")),
        "--locust",
        "http://127.0.0.1:9,http://127.0.0.1:9",
    ]
    extra += ["--k8s-namespace", "otb-demo-a,demo-b-ns"]
    try:
        p = subprocess.run(
            capture_argv(tmp_path, SCENARIO, out, extra),
            cwd=REPO, capture_output=True, text=True, env=capture_env(tmp_path, state_path), timeout=120,
        )  # fmt: skip
    finally:
        for proc, _, _ in flagd.values():
            proc.kill()
    assert p.returncode == 0, p.stdout + p.stderr

    sched = RealisedSchedule.from_yaml((out / "schedule.yaml").read_bytes())
    entries = {e.slot_id: e for e in sched.entries}
    f1, f2, d1 = entries["f1"], entries["f2"], entries["d1"]
    t0 = json.loads((out / "capture.json").read_text())["capture_start_ns"]
    kubectl = calls(log)
    at = {c["argv"]: c for c in kubectl}
    a_release, a_restore = at[set_env("otb-demo-a", RELEASED)], at[set_env("otb-demo-a", ORIGINAL)]
    a_ready = next(c for c in kubectl if c["argv"] == rollout("otb-demo-a"))
    a_flag = [json.loads(x) for x in flagd["a"][2].read_text().splitlines()]
    flag_on_ns = next(w["at_ns"] for w in a_flag if w["variants"]["intlShippingSlowdown"] == "5sec")
    flag_off_ns = next(w["at_ns"] for w in a_flag if w["at_ns"] > flag_on_ns)
    confirm_on, confirm_off = (a for a in f1.actions if a.action == "deploy.confirm")
    b_restore = next(a for a in f2.actions if a.action == "deploy.apply" and ORIGINAL in a.detail["argv"])
    result = {
        "exit_code": p.returncode,
        "status": {s: e.status for s, e in sorted(entries.items())},
        "f1": {
            "flag": f1.flag, "variant": f1.variant, "fault_class": f1.fault_class, "root_service": f1.root_service,
            "relation": f1.relation, "target": f1.target, "steps": steps(f1),
            "release": {k: v for k, v in confirm_on.detail.items() if k != "phase"},
        },
        "f2_steps": steps(f2),
        "d1": {"decoy_type": d1.decoy_type, "target": d1.target, "steps": steps(d1)},
        "f2_rollout_stderr": next(a.detail["stderr"].strip() for a in f2.actions if a.action == "deploy.rollout"),
        "kubectl_calls": sorted(c["argv"] for c in kubectl),
        "flag_writes": {i: flag_writes(flagd[i][2]) for i in ("a", "b")},
        "resource_env_after": {
            k: resource_env(state_path, k) for k in ("otb-demo-a/shipping", "otb-demo-a/quote", "demo-b-ns/shipping")
        },
        "flag_on_after_release_ready": flag_on_ns > a_ready["end_ns"],
        "flag_off_before_restore": flag_off_ns < a_restore["at_ns"],
    }  # fmt: skip
    windows = {s: [round((w - t0) / NS, 1) for w in (e.window.start, e.window.end)] for s, e in entries.items()}
    timing = {"window_offsets_s": windows, "f2_rollback_offset_s": round((b_restore.at_ns - t0) / NS, 1)}
    write_artifact(artifacts / "capture_k8s_deploy.json", result, timing=timing)

    assert (f1.window.start, f1.window.end) == (confirm_on.at_ns, confirm_off.at_ns), "release to rollback"
    assert a_release["at_ns"] < a_ready["at_ns"] < flag_on_ns < f1.window.start
    assert b_restore.at_ns < t0 + 6 * NS, "a release that never became ready is rolled back at once, not at slot end"
    a_on = [get("otb-demo-a"), get("otb-demo-a"), set_env("otb-demo-a", RELEASED), rollout("otb-demo-a")]
    a_off = [get("otb-demo-a"), get("otb-demo-a"), set_env("otb-demo-a", ORIGINAL), rollout("otb-demo-a")]
    b_on = [get("demo-b-ns"), get("demo-b-ns"), set_env("demo-b-ns", RELEASED), rollout("demo-b-ns")]
    b_undo = [get("demo-b-ns"), get("demo-b-ns"), set_env("demo-b-ns", ORIGINAL), rollout("demo-b-ns")]
    b_off = [get("demo-b-ns"), rollout("demo-b-ns")]
    q = [get("otb-demo-a", "quote"), get("otb-demo-a", "quote"), rollout("otb-demo-a", "quote")]
    preflight = [get("otb-demo-a"), get("demo-b-ns"), get("otb-demo-a", "quote")]
    d1_on, d1_off = [*q, set_env("otb-demo-a", RELEASED, "quote")], [*q, set_env("otb-demo-a", ORIGINAL, "quote")]
    assert result == {
        "exit_code": 0,
        "status": {"d1": "realised", "f1": "realised", "f2": "failed"},
        "f1": {
            "flag": "intlShippingSlowdown",
            "variant": "5sec",
            "fault_class": "bad_deploy",
            "root_service": "shipping",
            "relation": "deploy",
            "target": "shipping",
            "steps": [
                ["deploy.read", "on", True],
                ["deploy.apply", "on", True],
                ["deploy.rollout", "on", True],
                ["flag.write", "on", True],
                ["flag.confirm", "on", True],
                ["deploy.confirm", "on", True],
                ["flag.write", "off", True],
                ["flag.confirm", "off", True],
                ["deploy.apply", "off", True],
                ["deploy.rollout", "off", True],
                ["deploy.confirm", "off", True],
            ],
            "release": {
                "deployment": "shipping",
                "from_version": "3.0.0",
                "to_version": "3.0.1",
                "flag": "intlShippingSlowdown",
                "variant": "5sec",
            },
        },
        "f2_steps": [
            ["deploy.read", "on", True],
            ["deploy.apply", "on", True],
            ["deploy.rollout", "on", False],
            ["deploy.apply", "on", True],
            ["deploy.rollout", "on", True],
            ["deploy.confirm", "on", False],
            ["flag.write", "off", True],
            ["flag.confirm", "off", True],
            ["deploy.rollout", "off", True],
            ["deploy.confirm", "off", True],
        ],
        "d1": {
            "decoy_type": "benign_deploy",
            "target": "quote",
            "steps": [
                ["deploy.read", "on", True],
                ["deploy.apply", "on", True],
                ["deploy.rollout", "on", True],
                ["deploy.confirm", "on", True],
                ["deploy.apply", "off", True],
                ["deploy.rollout", "off", True],
                ["deploy.confirm", "off", True],
            ],
        },
        "f2_rollout_stderr": "error: timed out waiting for the condition",
        "kubectl_calls": sorted([*preflight, *a_on, *a_off, *b_on, *b_undo, *b_off, *d1_on, *d1_off]),
        "flag_writes": {"a": ["5sec", "off"], "b": ["off"]},
        "resource_env_after": {
            "otb-demo-a/shipping": ORIGINAL,
            "otb-demo-a/quote": ORIGINAL,
            "demo-b-ns/shipping": ORIGINAL,
        },
        "flag_on_after_release_ready": True,
        "flag_off_before_restore": True,
    }


def wait_for_event(events: Path, action: str, phase: str, proc: subprocess.Popen, timeout_s: float = 60) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if events.exists() and any(
            (e := json.loads(line))["action"] == action and e["phase"] == phase and e["ok"]
            for line in events.read_text().splitlines()
        ):
            return
        assert proc.poll() is None, f"the capture exited before {action} {phase}"
        time.sleep(0.05)
    raise AssertionError(f"no {action} {phase} within {timeout_s} s")


def test_k8s_bad_deploy_rolls_back_after_crashes(tmp_path: Path, artifacts: Path) -> None:
    """SIGKILL the capture while the release's rollout runs (the change is made, nothing is confirmed), and again
    after the release is confirmed; the third run rolls back to the value recorded before the first change."""
    state_path, log = cluster(
        tmp_path,
        {
            "deployments": {"otb-demo-a/shipping": deployment("otb-demo-a")},
            "rollout_delay_s": {"otb-demo-a": {"3.0.1": 3}},
        },
    )
    proc, url, writes = start_flagd(tmp_path, "a")
    out = tmp_path / "resume"
    argv = capture_argv(tmp_path, RESUME, out, ["--flagd-ui", url, "--locust", "http://127.0.0.1:9"])
    env = capture_env(tmp_path, state_path)
    events = out / "events.jsonl"
    try:
        run1 = subprocess.Popen(argv, cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_for_event(events, "deploy.apply", "on", run1)
        run1.send_signal(signal.SIGKILL)
        run1.wait()
        after_crash1 = resource_env(state_path, "otb-demo-a/shipping")
        run2 = subprocess.Popen(argv, cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_for_event(events, "deploy.confirm", "on", run2)
        run2.send_signal(signal.SIGKILL)
        run2.wait()
        p = subprocess.run(argv, cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    finally:
        proc.kill()
    assert p.returncode == 0, p.stdout + p.stderr

    f1 = RealisedSchedule.from_yaml((out / "schedule.yaml").read_bytes()).entries[0]
    reads = [a for a in f1.actions if a.action == "deploy.read"]
    result = {
        "after_first_crash": after_crash1,
        "status": f1.status,
        "steps": steps(f1),
        "recorded_original": [a.detail["original"] for a in reads],
        "set_env_values": [
            c["argv"].split("OTEL_RESOURCE_ATTRIBUTES=")[1] for c in calls(log) if " set env " in c["argv"]
        ],
        "resource_env_after": resource_env(state_path, "otb-demo-a/shipping"),
        "flag_writes": flag_writes(writes),
    }
    write_artifact(artifacts / "capture_k8s_deploy_resume.json", result)
    assert result == {
        "after_first_crash": RELEASED,
        "status": "realised",
        "steps": [
            ["deploy.read", "on", True],
            ["deploy.apply", "on", True],
            ["deploy.apply", "on", True],
            ["deploy.rollout", "on", True],
            ["flag.write", "on", True],
            ["flag.confirm", "on", True],
            ["deploy.confirm", "on", True],
            ["flag.write", "off", True],
            ["flag.confirm", "off", True],
            ["deploy.apply", "off", True],
            ["deploy.rollout", "off", True],
            ["deploy.confirm", "off", True],
        ],
        "recorded_original": [ORIGINAL],
        "set_env_values": [RELEASED, RELEASED, ORIGINAL],
        "resource_env_after": ORIGINAL,
        "flag_writes": ["10sec", "off"],
    }


def test_k8s_bad_deploy_rolls_back_a_release_whose_window_ended_during_a_crash(tmp_path: Path) -> None:
    """SIGKILL the capture while the release's rollout runs, and restart it only after the slot has ended: the
    release is skipped and failed, and its rollback still runs."""
    state_path, log = cluster(
        tmp_path,
        {
            "deployments": {"otb-demo-a/shipping": deployment("otb-demo-a")},
            "rollout_delay_s": {"otb-demo-a": {"3.0.1": 3}},
        },
    )
    proc, url, writes = start_flagd(tmp_path, "a")
    out = tmp_path / "late"
    argv = capture_argv(tmp_path, LATE, out, ["--flagd-ui", url, "--locust", "http://127.0.0.1:9"])
    env = capture_env(tmp_path, state_path)
    try:
        run1 = subprocess.Popen(argv, cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_for_event(out / "events.jsonl", "deploy.apply", "on", run1)
        run1.send_signal(signal.SIGKILL)
        run1.wait()
        t0 = json.loads((out / "capture.json").read_text())["capture_start_ns"]
        time.sleep(max(0.0, (t0 + 6 * NS - time.time_ns()) / NS))
        p = subprocess.run(argv, cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    finally:
        proc.kill()
    assert p.returncode == 0, p.stdout + p.stderr
    f1 = RealisedSchedule.from_yaml((out / "schedule.yaml").read_bytes()).entries[0]
    assert (f1.status, steps(f1)) == (
        "failed",
        [
            ["deploy.read", "on", True],
            ["deploy.apply", "on", True],
            ["skip", "on", False],
            ["flag.write", "off", True],
            ["flag.confirm", "off", True],
            ["deploy.apply", "off", True],
            ["deploy.rollout", "off", True],
            ["deploy.confirm", "off", True],
        ],
    )
    assert resource_env(state_path, "otb-demo-a/shipping") == ORIGINAL
    assert [c["argv"] for c in calls(log) if " set env " in c["argv"]] == [
        set_env("otb-demo-a", RELEASED),
        set_env("otb-demo-a", ORIGINAL),
    ]
    assert flag_writes(writes) == ["off"]


def test_k8s_bad_deploy_refuses_outside_the_demo(tmp_path: Path) -> None:
    """kube-system holds a shipping Deployment that is not the demo's; the capture must not start, and no change
    may reach the cluster. The Compose driver cannot deploy at all."""
    state = {
        "deployments": {
            "kube-system/shipping": deployment("kube-system", {"app.kubernetes.io/name": "shipping"}),
            "otb-demo-a/shipping": deployment("otb-demo-a"),
        }
    }
    state_path, log = cluster(tmp_path, state)
    proc, url, _ = start_flagd(tmp_path, "a")
    try:
        extra = ["--flagd-ui", url, "--locust", "http://127.0.0.1:9", "--k8s-namespace", "kube-system"]
        env = capture_env(tmp_path, state_path)
        p = subprocess.run(
            capture_argv(tmp_path, REFUSED, tmp_path / "refused", extra),
            cwd=REPO, capture_output=True, text=True, env=env, timeout=120,
        )  # fmt: skip
        c = subprocess.run(
            capture_argv(tmp_path, REFUSED, tmp_path / "compose", ["--flagd-ui", url], driver="compose"),
            cwd=REPO, capture_output=True, text=True, env=env, timeout=120,
        )  # fmt: skip
    finally:
        proc.kill()
    assert p.returncode == 1, p.stdout + p.stderr
    assert (
        "f1: a: deployment/shipping in kube-system is not an OpenTelemetry Demo Deployment"
        " (app.kubernetes.io/part-of is None, not 'opentelemetry-demo'); refusing to change it" in p.stderr
    ), p.stderr
    assert [x["argv"] for x in calls(log)] == [get("kube-system")]
    assert json.loads(state_path.read_text()) == state, "the cluster is unchanged"
    assert not (tmp_path / "refused" / "events.jsonl").exists()
    assert c.returncode == 1, c.stdout + c.stderr
    assert "f1: a deploy fault needs --decoy-driver k8s" in c.stderr, c.stderr
    (tmp_path / "unversioned.yaml").write_text(UNVERSIONED)
    u = subprocess.run(
        [sys.executable, "-m", "otb.cli", "scenario", "plan", str(tmp_path / "unversioned.yaml")],
        cwd=REPO, capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert (u.returncode, u.stdout) == (1, ""), u.stdout + u.stderr
    assert "deploy slot f1 needs params.version_to and no group" in u.stderr, u.stderr


class Ofrep:
    """flagd's OFREP evaluation endpoint, stuck serving intlShippingSlowdown at 5sec: a flag write is never seen."""

    def __init__(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: object) -> None:
                pass

            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                flag = self.path.rsplit("/", 1)[-1]
                body = json.dumps({"key": flag, "variant": "5sec" if flag == "intlShippingSlowdown" else "off"})
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body.encode())

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def test_k8s_bad_deploy_rolls_back_when_stopped(tmp_path: Path) -> None:
    """SIGTERM while the release is out, with flagd still serving the flag on, so the rollback's flag-off never
    confirms before the capture stops: the Deployment is restored anyway, and the rollback stays unconfirmed for
    a rerun to finish."""
    state_path, log = cluster(tmp_path, {"deployments": {"otb-demo-a/shipping": deployment("otb-demo-a")}})
    proc, url, _ = start_flagd(tmp_path, "a")
    ofrep = Ofrep()
    out = tmp_path / "stopped"
    extra = ["--flagd-ui", url, "--ofrep", ofrep.url, "--locust", "http://127.0.0.1:9"]
    try:
        run = subprocess.Popen(
            capture_argv(tmp_path, STOPPED, out, extra),
            cwd=REPO, env=capture_env(tmp_path, state_path), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )  # fmt: skip
        wait_for_event(out / "events.jsonl", "deploy.confirm", "on", run)
        run.send_signal(signal.SIGTERM)
        _, stderr = run.communicate(timeout=60)
    finally:
        proc.kill()
        ofrep.server.shutdown()
    events = [json.loads(line) for line in (out / "events.jsonl").read_text().splitlines()]
    assert [[e["action"], e["phase"], e["ok"]] for e in events if e["slot_id"] == "f1"] == [
        ["deploy.read", "on", True],
        ["deploy.apply", "on", True],
        ["deploy.rollout", "on", True],
        ["flag.write", "on", True],
        ["flag.confirm", "on", True],
        ["deploy.confirm", "on", True],
        ["flag.write", "off", True],
        ["deploy.apply", "off", True],
        ["deploy.rollout", "off", True],
    ], stderr.decode()
    assert resource_env(state_path, "otb-demo-a/shipping") == ORIGINAL
    assert [c["argv"] for c in calls(log) if " set env " in c["argv"]] == [
        set_env("otb-demo-a", RELEASED),
        set_env("otb-demo-a", ORIGINAL),
    ]
    assert not (out / "schedule.yaml").exists(), "a stopped capture is not sealed"


def test_k8s_bad_deploy_never_rolls_back_over_a_later_release(tmp_path: Path) -> None:
    """Every rollout of the original version fails, so s1's rollback stays open and is retried. s2 then releases the
    same Deployment: s1's retries leave s2's flag and version alone, s1 closes as superseded, and only s2's own
    rollback puts the original back."""
    state_path, log = cluster(
        tmp_path,
        {"deployments": {"otb-demo-a/shipping": deployment("otb-demo-a")}, "never_ready": {"otb-demo-a": "3.0.0"}},
    )
    proc, url, writes = start_flagd(tmp_path, "a")
    out = tmp_path / "superseded"
    argv = capture_argv(tmp_path, SUPERSEDED, out, ["--flagd-ui", url, "--locust", "http://127.0.0.1:9"])
    try:
        p = subprocess.run(
            argv, cwd=REPO, env=capture_env(tmp_path, state_path), capture_output=True, text=True, timeout=120
        )
    finally:
        proc.kill()
    assert p.returncode == 0, p.stdout + p.stderr
    entries = {e.slot_id: e for e in RealisedSchedule.from_yaml((out / "schedule.yaml").read_bytes()).entries}
    s1, s2 = entries["s1"], entries["s2"]
    s2_on = next(a.at_ns for a in s2.actions if a.action == "deploy.confirm" and a.detail["phase"] == "on")
    s2_off = json.loads((out / "capture.json").read_text())["capture_start_ns"] + 8 * NS  # its planned off
    s1_close = next(
        a for a in s1.actions if a.action == "deploy.confirm" and a.detail["phase"] == "off" and "note" in a.detail
    )
    flag_ns = [json.loads(line)["at_ns"] for line in writes.read_text().splitlines()]
    v = "service.namespace=opentelemetry-demo,service.version=3.0.2,service.criticality=high"
    assert (s1.status, s2.status) == ("failed", "failed"), "neither rollback got ready"
    assert s1_close.detail["note"] == "superseded: another change replaced this release before its rollback"
    assert s2_on < s1_close.at_ns < s2_off, "s1 is closed while s2 is out"
    assert [t for t in flag_ns if s2_on < t < s2_off] == [], "nothing turned s2's flag off while it was out"
    assert [c["argv"] for c in calls(log) if " set env " in c["argv"]] == [
        set_env("otb-demo-a", RELEASED),
        set_env("otb-demo-a", ORIGINAL),
        set_env("otb-demo-a", v),
        set_env("otb-demo-a", ORIGINAL),
    ]
    assert resource_env(state_path, "otb-demo-a/shipping") == ORIGINAL
