"""The split cell: dispatch hands each cell to a tools worker and an agent worker that run as separate processes
with separate environments and scratch roots, as the three containers of the runner pod do (otb/run/workers.py).

Ways this could fail, listed before the code:
1. the agent process sees the tools side's credentials, the operator's environment, or a HOME that outlives the cell;
2. a split run grades differently from the in-process run of the same fixture;
3. no manifest hash is recorded, cells of one stack record different ones, or selfcheck accepts a tampered one;
4. a vendor server's sampling request reaches the agent, or is not logged;
5. wall_s includes the vendor server's start-up, or the clock starts before the tools side is ready;
6. dispatch waits out the wall budget when no worker is running, or when a stack's server cannot start;
7. the proxy's HTTP endpoint serves a request without this cell's bearer token, or with a foreign Origin;
8. a Claude Code cell that ran in its empty directory is marked invalid, or one that ran elsewhere passes, because
   the runner re-checks the init event after the agent side has scrubbed its paths;
9. the model API refuses the cell (a bad key, a subscription's usage cap) and the cell is graded as the agent's
   wrong answer instead of being marked invalid;
10. a subscription token given to the agent worker does not reach the CLI, or is written anywhere in the run;
11. `parallel` cells per stack run one at a time, or more than `parallel` overlap, or a cell runs twice;
12. `order_seed` orders the instances differently on two stacks, or not at all;
13. a subscription's usage limit uses up a cell's invalid retries, so every cell queued behind it ends invalid;
14. after a usage limit, a cell on any stack starts before the limit resets (one subscription serves every stack);
15. an attempt the usage limit refused is lost, instead of kept on record outside the graded attempts;
16. a refusal that is not a usage limit (a bad key, no credit) pauses the run instead of marking the cell invalid;
17. two runs dispatching the same cell ids at once share one directory per cell, so one run's job replaces the
    other's in-flight cell and a run collects an answer its own agent never gave;
18. a run's answers come from the other run's agent, or a cell of either run runs twice or not at all;
19. a cell directory that vanishes while dispatch waits on it crashes the dispatcher (writing stop, collecting)
    instead of becoming an invalid attempt that retry_invalid reruns, or dispatch waits out the cell's deadline;
20. a worker that imported other harness code than the dispatcher's checkout (the checkout moved after the worker
    started) is handed cells;
21. a worker that reports no harness code at all (one from before the report) is taken as current;
22. dispatch refuses workers that run the dispatcher's own code, or needs git on the worker side to tell (the
    runner pod's worker containers mount otb/ without .git, on images without git).

Artifacts: tests/e2e/artifacts/cell_workers.json, .otb/e2e/run-split-oracle.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from conftest import FIXTURES, REPO, TOKEN, keep, ok, otb, write_artifact, write_config

from otb.run.config import parse_run_config
from otb.run.dispatch import CellSpec, cell_file
from otb.run.workers import JOB, STOP, heartbeat_fresh, read_json, submit_job
from otb.stacks.definition import load_surface
from otb.tasks.suite import load_instances, read_jsonl

TIMING = ("wall_s", "call_secs")


class Workers:
    """Worker processes, one per role given, each with exactly the environment given. A role in `code` imports otb
    from that directory instead of this checkout."""

    def __init__(self, root: Path, envs: dict[str, dict[str, str]], code: dict[str, Path] | None = None):
        root.mkdir(parents=True, exist_ok=True)
        self.cells = root / "cells"
        self.scratch = {role: root / f"scratch-{role}" for role in envs}
        self.logs = {role: (root / f"{role}.log").open("w") for role in envs}
        self.procs = {
            role: subprocess.Popen(
                [sys.executable, "-m", "otb.run.workers", "--role", role, "--cells", self.cells, "--scratch", s],
                env={"PATH": os.environ["PATH"], **env},
                cwd=(code or {}).get(role, root),
                stdout=self.logs[role],
                stderr=subprocess.STDOUT,
            )
            for (role, env), s in zip(envs.items(), self.scratch.values(), strict=True)
        }
        deadline = time.time() + 30
        while not all(heartbeat_fresh(self.cells, r) for r in self.procs) and time.time() < deadline:
            time.sleep(0.1)
        assert all(heartbeat_fresh(self.cells, r) for r in self.procs), "workers did not start"

    def stop(self) -> None:
        for p in self.procs.values():
            p.kill()
            p.wait()
        for f in self.logs.values():
            f.close()


def pod_envs(tmp_path: Path, env: dict) -> dict[str, dict[str, str]]:
    """What the pod gives each role: the tools side alone holds the stack's credentials."""
    return {
        "tools": {
            "HOME": str(tmp_path / "tools-home"),
            "FIXTURE_TOKEN": env["FIXTURE_TOKEN"],
            "FIXTURE_HTTP_URL": env["FIXTURE_HTTP_URL"],
        },
        "agent": {"HOME": str(tmp_path / "agent-home"), "OTB_AGENT_WORKER_CANARY": "worker-only"},
    }


def dispatch_split(monkeypatch: pytest.MonkeyPatch, w: Workers) -> None:
    """The dispatcher's view: split mode, the workers' directory, and no stack credential at all."""
    monkeypatch.setenv("OTB_ISOLATION", "split")
    monkeypatch.setenv("OTB_CELLS_DIR", str(w.cells))
    monkeypatch.delenv("FIXTURE_TOKEN")
    monkeypatch.delenv("FIXTURE_HTTP_URL")


@pytest.fixture
def workers(tmp_path: Path, env: dict, monkeypatch: pytest.MonkeyPatch) -> Iterator[Workers]:
    w = Workers(tmp_path / "w", pod_envs(tmp_path, env))
    dispatch_split(monkeypatch, w)
    yield w
    w.stop()


def run_all(run: Path, cfg: Path) -> None:
    ok(otb("run", "new", run, "--config", cfg))
    ok(otb("run", "register", run))
    ok(otb("run", "dispatch", run))
    ok(otb("run", "grade", run))
    ok(otb("run", "report", run))


def graded(run: Path) -> dict[str, dict]:
    return {
        r["cell"]: {k: v for k, v in r.items() if k not in TIMING} for r in read_jsonl(run / "grade" / "runs.jsonl")
    }


def metas(run: Path) -> dict[str, dict]:
    return {m["cell"]: m for m in (json.loads(p.read_text()) for p in sorted((run / "cells").glob("*/meta.json")))}


def stacks_with_server(tmp_path: Path, script: Path) -> Path:
    stacks = tmp_path / "stacks"
    base = yaml.safe_load((FIXTURES / "stacks/fixture-stdio/stack.yaml").read_text())
    (stacks / "fixture-stdio").mkdir(parents=True)
    (stacks / "fixture-stdio" / "stack.yaml").write_text(
        yaml.safe_dump({**base, "mcp": {**base["mcp"], "argv": ["${OTB_PYTHON}", str(script)]}})
    )
    shutil.copytree(FIXTURES / "stacks/fixture-http", stacks / "fixture-http")
    return stacks


def one_stack_config(tmp_path: Path, name: str, stacks: Path) -> Path:
    cfg = write_config(tmp_path / f"{name}.yaml", f"scratch-{name}", {"adapter": "oracle"}, stacks=("fixture-stdio",))
    raw = yaml.safe_load(cfg.read_text())
    cfg.write_text(
        yaml.safe_dump({**raw, "stacks_root": str(stacks), "instances": ["fx-l1-count--a1"]}, sort_keys=False)
    )
    return cfg


def test_split_run_matches_in_process_and_isolates_the_agent(
    tmp_path: Path, env: dict, monkeypatch: pytest.MonkeyPatch, artifacts: Path
) -> None:
    in_process = tmp_path / "scratch-inproc"
    run_all(in_process, write_config(tmp_path / "a.yaml", "scratch-inproc", {"adapter": "oracle"}))

    w = Workers(tmp_path / "w", pod_envs(tmp_path, env))
    try:
        dispatch_split(monkeypatch, w)
        split = tmp_path / "scratch-split"
        run_all(split, write_config(tmp_path / "b.yaml", "scratch-split", {"adapter": "oracle"}))
    finally:
        w.stop()

    want, got = graded(in_process), graded(split)
    assert sorted(want) == sorted(got) and {r["outcome"] for r in got.values()} == {"correct", "excluded"}
    differ = {c: {k: (want[c].get(k), got[c].get(k)) for k in want[c] if want[c].get(k) != got[c].get(k)} for c in want}
    assert {c: d for c, d in differ.items() if d} == {}, "split and in-process grade differently"

    ran = {c: m for c, m in metas(split).items() if m["status"] == "done"}
    assert len(ran) == 6 and all(m["valid"] and m["isolation"] == "split" for m in ran.values())
    assert all(m["probe_s"] > 0 and m["tools_ready_utc"] <= m["started_utc"] for m in ran.values())
    assert all(m["credential_source"] == "none" for m in ran.values())
    hashes = {m["manifest_sha256"] for m in ran.values()}
    first = "fixture-stdio--fx-l1-count--a1--r1"
    assert hashes == {metas(in_process)[first]["manifest_sha256"]}, "one fixture server, one manifest, both modes"

    cell = split / "cells" / first
    init = next(e for e in read_jsonl(cell / "transcript.jsonl") if e["type"] == "init")
    # macOS adds __CF_USER_TEXT_ENCODING to every process it launches, and Python's C-locale coercion sets LC_CTYPE
    seen = {k for k in init["env_keys"] if not k.startswith("__CF") and k != "LC_CTYPE"}
    assert seen == {"PATH", "HOME", "TMPDIR"}, "the agent process got exactly PATH, a fresh HOME and TMPDIR"
    assert (init["cwd"], init["home"]) == ("$CWD", "$HOME")
    status = json.loads((cell / "agent.status.json").read_text())
    scratch = str(w.scratch["agent"].resolve())
    assert status["cwd"].startswith(scratch) and status["home"].startswith(scratch)
    assert not Path(status["home"]).exists() and not any(w.scratch["agent"].iterdir()), "the cell's HOME outlived it"
    assert sorted(status["env_keys"]) == ["HOME", "PATH", "TMPDIR"]
    leaked = [p.name for p in (split / "cells").rglob("*") if p.is_file() and TOKEN in p.read_text(errors="replace")]
    assert leaked == [], "the stack credential reached a cell"
    assert [p.name for p in w.cells.iterdir() if p.is_dir()] == [], "shared cell directories left behind"

    ok(otb("run", "selfcheck", split))
    checks = json.loads((split / "selfcheck.json").read_text())["checks"]
    assert checks["one_manifest_per_stack"] == {
        "status": "pass",
        "manifests": {"fixture-http": sorted(hashes), "fixture-stdio": sorted(hashes)},
        "divergent_stacks": [],
        "cells_without_manifest": [],
    }
    keep(split, "run-split-oracle")
    meta_path = cell / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta_path.write_text(json.dumps({**meta, "manifest_sha256": "0" * 64}))
    assert otb("run", "selfcheck", split).returncode == 1
    tampered = json.loads((split / "selfcheck.json").read_text())["checks"]["one_manifest_per_stack"]
    assert (tampered["status"], tampered["divergent_stacks"]) == ("fail", ["fixture-stdio"])
    write_artifact(
        artifacts / "cell_workers.json",
        {
            "cells_graded_identically": sorted(want),
            "agent_env_keys": sorted(status["env_keys"]),
            "credential_source": status["credential_source"],
            "one_manifest_per_stack": checks["one_manifest_per_stack"]["status"],
            "tampered_manifest_fails_selfcheck": tampered["status"] == "fail",
            "excluded": sorted(c for c, m in metas(split).items() if m["status"] == "excluded"),
        },
        timing={c: {"probe_s": m["probe_s"], "wall_s": m["wall_s"]} for c, m in ran.items()},
    )


def test_sampling_is_logged_and_refused_and_the_endpoint_needs_the_token(tmp_path: Path, env: dict) -> None:
    """Acting as the agent side by hand: a tools worker serves one cell, raw requests probe the endpoint's checks,
    and the scripted client calls the fixture's ask_model tool through it."""
    w = Workers(tmp_path / "w", {"tools": pod_envs(tmp_path, env)["tools"]})
    try:
        cfg = parse_run_config(write_config(tmp_path / "run.yaml", "scratch-x", {"adapter": "oracle"}))
        stack = load_surface(FIXTURES / "stacks" / "fixture-stdio")
        inst = load_instances(FIXTURES / "suite-mini" / "instances.jsonl")["fx-l1-count--a1"]
        spec = CellSpec("fixture-stdio--fx-l1-count--a1--r1", "fixture-stdio", inst.instance_id, 1, None)
        job = {"kind": "cell", "stack": "fixture-stdio", "token": "t1"}
        d = submit_job(w.cells, spec.cell_id, {"cell.json": cell_file(cfg, stack, inst, spec)}, job)
        endpoint = None
        deadline = time.time() + 60
        while endpoint is None and time.time() < deadline:
            endpoint = read_json(d / "endpoint.json")
            time.sleep(0.1)
        assert endpoint is not None, read_json(d / "tools.status.json")
        assert endpoint["url"].startswith("http://127.0.0.1:") and len(endpoint["token"]) >= 32

        def post(body: dict, token: str | None = endpoint["token"], origin: str | None = None) -> tuple[int, dict]:
            headers = {"Content-Type": "application/json"}
            if token is not None:
                headers["Authorization"] = f"Bearer {token}"
            if origin is not None:
                headers["Origin"] = origin
            req = urllib.request.Request(
                endpoint["url"], data=json.dumps(body).encode(), headers=headers, method="POST"
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return resp.status, json.loads(resp.read() or b"null")
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read() or b"null")

        listing = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        assert post(listing, token=None)[0] == 401
        assert post(listing, token="not-the-token")[0] == 401
        assert post(listing, origin="https://evil.example")[0] == 403
        status, listed = post(listing, origin="http://localhost:3000")
        assert status == 200 and "ask_model" in [t["name"] for t in listed["result"]["tools"]]

        server = {"type": "http", "url": endpoint["url"], "headers": {"Authorization": f"Bearer {endpoint['token']}"}}
        (tmp_path / "mcp.json").write_text(json.dumps({"mcpServers": {"telemetry": server}}))
        calls = [{"name": "ask_model", "arguments": {}}, {"name": "list_services", "arguments": {}}]
        (tmp_path / "plan.json").write_text(json.dumps({"calls": calls, "answer": {"error_span_count": 42}}))
        with (tmp_path / "transcript.jsonl").open("w") as out:
            subprocess.run(
                [sys.executable, "-m", "otb.run.agents.scripted", tmp_path / "plan.json", tmp_path / "mcp.json"],
                stdout=out,
                check=True,
                timeout=120,
            )
        (d / STOP).touch()
        deadline = time.time() + 30
        while not (d / "tools.status.json").exists() and time.time() < deadline:
            time.sleep(0.1)
        tools_status = json.loads((d / "tools.status.json").read_text())
        log = read_jsonl(d / "calls.jsonl")
        answer = json.loads((d / "answer.json").read_text())
    finally:
        w.stop()

    assert tools_status["state"] == "done" and len(tools_status["manifest_sha256"]) == 64
    transcript = read_jsonl(tmp_path / "transcript.jsonl")
    asked = next(e for e in transcript if e.get("tool") == "ask_model")
    assert asked["is_error"] is True and "not available to this server" in asked["text"]
    requests = [{k: c[k] for k in ("method", "disposition")} for c in log if c["kind"] == "server_request"]
    assert requests == [{"method": "sampling/createMessage", "disposition": "refused"}]
    refused = [c["reason"] for c in log if c["kind"] == "http_refused"]
    assert refused == ["bad bearer token", "bad bearer token", "origin 'https://evil.example' is not loopback"]
    by_tool = {c["tool"]: c for c in log if c["kind"] == "call"}
    assert by_tool["ask_model"]["error"] is True and by_tool["list_services"]["error"] is False
    assert next(e for e in transcript if e["type"] == "submit")["is_error"] is False and answer == {
        "error_span_count": 42
    }
    assert next(c for c in log if c["kind"] == "manifest")["manifest_sha256"] == tools_status["manifest_sha256"]
    assert next(c for c in log if c["kind"] == "initialize_request")["client"]["name"] == "otb-scripted"
    assert [c["kind"] for c in log][:2] == ["initialize", "manifest"], "the proxy handshook before any client"


def test_wall_clock_starts_after_the_server_is_up(tmp_path: Path, workers: Workers) -> None:
    (tmp_path / "slow.py").write_text(
        "import runpy, sys, time\n"
        "time.sleep(3)\n"
        f"sys.argv = [{str(FIXTURES / 'mcp_fixture_server.py')!r}]\n"
        f"runpy.run_path({str(FIXTURES / 'mcp_fixture_server.py')!r}, run_name='__main__')\n"
    )
    run = tmp_path / "scratch-slow"
    run_all(run, one_stack_config(tmp_path, "slow", stacks_with_server(tmp_path, tmp_path / "slow.py")))
    meta = metas(run)["fixture-stdio--fx-l1-count--a1--r1"]
    assert meta["valid"] and meta["probe_s"] >= 3 and meta["wall_s"] < 3, (meta["probe_s"], meta["wall_s"])
    assert read_jsonl(run / "grade" / "runs.jsonl")[0]["outcome"] == "correct"


FAKE_CLAUDE = """#!{python}
import json, os, sys
if "--version" in sys.argv:
    print("{version} (Claude Code)")
    sys.exit(0)
emit = lambda e: print(json.dumps(e, separators=(",", ":")), flush=True)
emit({{"type": "system", "subtype": "init", "cwd": {cwd}, "session_id": "fake", "claude_code_version": "{version}",
      "model": sys.argv[sys.argv.index("--model") + 1], "permissionMode": "default",
      "tools": ["mcp__telemetry__submit_answer"], "mcp_servers": [{{"name": "telemetry", "status": "connected"}}],
      "plugins": [], "memory_paths": {{}}, "skills": [],
      "apiKeySource": "token" if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") == {token!r} else "none"}})
emit({result})
"""
ANSWERED = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "usage": {}}
# the shape Claude Code 2.1.295 wrote on GKE when the API refused a key without credit
REFUSED = {
    "type": "result",
    "subtype": "success",
    "is_error": True,
    "num_turns": 1,
    "api_error_status": 400,
    "terminal_reason": "api_error",
    "result": "API Error: 400 Your credit balance is too low to access the Anthropic API.",
    "usage": {},
}
SUBSCRIPTION_TOKEN = "sk-ant-oat01-fixture-subscription-token"


def run_fake_claude(
    tmp_path: Path, env: dict, monkeypatch: pytest.MonkeyPatch, cwd: str, result: dict, agent_env: dict[str, str]
) -> tuple[Path, dict]:
    """One split cell of the claude-code adapter against a fake CLI, and that cell's meta."""
    version = "2.1.295"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(
        FAKE_CLAUDE.format(python=sys.executable, version=version, cwd=cwd, token=SUBSCRIPTION_TOKEN, result=result)
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    agent = {"adapter": "claude-code", "model": "claude-haiku-4-5-20251001", "cli_version": version}
    cfg = write_config(tmp_path / "c.yaml", "scratch-split-claude", agent, stacks=("fixture-stdio",))
    raw = yaml.safe_load(cfg.read_text())
    cfg.write_text(yaml.safe_dump({**raw, "instances": ["fx-l1-count--a1"]}, sort_keys=False))
    envs = pod_envs(tmp_path, env)
    envs["agent"] |= agent_env
    w = Workers(tmp_path / "w", envs)
    run = tmp_path / "scratch-split-claude"
    try:
        dispatch_split(monkeypatch, w)
        ok(otb("run", "new", run, "--config", cfg))
        ok(otb("run", "register", run))
        ok(otb("run", "dispatch", run))
    finally:
        w.stop()
    return run, metas(run)["fixture-stdio--fx-l1-count--a1--r1"]


@pytest.mark.parametrize(
    ("reported_cwd", "want"),
    [("os.getcwd()", []), ("os.path.dirname(os.getcwd())", ["cwd is not the cell's empty directory"])],
    ids=["in-its-cwd", "elsewhere"],
)
def test_split_claude_code_cell_is_checked_on_the_cwd_it_ran_in(
    tmp_path: Path, env: dict, monkeypatch: pytest.MonkeyPatch, reported_cwd: str, want: list[str]
) -> None:
    run, meta = run_fake_claude(tmp_path, env, monkeypatch, reported_cwd, ANSWERED, {})
    assert (meta["isolation"], meta["violations"], meta["valid"]) == ("split", want, not want)
    init = next(e for e in read_jsonl(run / "cells" / meta["cell"] / "transcript.jsonl") if e.get("subtype") == "init")
    assert (init["cwd"] == "$CWD") == (not want), init["cwd"]


@pytest.mark.parametrize(
    ("result", "want"),
    [(ANSWERED, []), (REFUSED, ["model API refused the request (400)"])],
    ids=["answered", "refused"],
)
def test_split_claude_code_cell_on_a_subscription_token_is_invalid_when_the_api_refuses(
    tmp_path: Path, env: dict, monkeypatch: pytest.MonkeyPatch, result: dict, want: list[str]
) -> None:
    token_file = tmp_path / "credentials" / "oauth-token"
    token_file.parent.mkdir()
    token_file.write_text(SUBSCRIPTION_TOKEN + "\n")
    agent_env = {"OTB_CLAUDE_OAUTH_TOKEN_FILE": str(token_file)}
    run, meta = run_fake_claude(tmp_path, env, monkeypatch, "os.getcwd()", result, agent_env)
    assert (meta["credential_source"], meta["api_key_source"]) == ("oauth_token_file", "token")
    assert "CLAUDE_CODE_OAUTH_TOKEN" in meta["env_keys"]
    assert (meta["violations"], meta["valid"]) == (want, not want)
    leaked = [p for p in run.rglob("*") if p.is_file() and SUBSCRIPTION_TOKEN.encode() in p.read_bytes()]
    assert leaked == [], "the subscription token was written into the run"


def test_dispatch_refuses_fast_without_workers_or_with_a_dead_server(
    tmp_path: Path, workers: Workers, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / "scratch-noworker"
    ok(otb("run", "new", run, "--config", write_config(tmp_path / "n.yaml", "scratch-noworker", {"adapter": "oracle"})))
    ok(otb("run", "register", run))
    (tmp_path / "nobody-here").mkdir()
    monkeypatch.setenv("OTB_CELLS_DIR", str(tmp_path / "nobody-here"))
    t0 = time.monotonic()
    p = otb("run", "dispatch", run)
    assert p.returncode == 2 and "no live tools or agent worker" in p.stderr and time.monotonic() - t0 < 10
    monkeypatch.setenv("OTB_CELLS_DIR", str(workers.cells))

    # run.yaml alone selects split (the config field, not a raw key dispatch reads), and refuses a third mode
    monkeypatch.delenv("OTB_ISOLATION")
    monkeypatch.delenv("OTB_CELLS_DIR")
    run = tmp_path / "scratch-yaml-split"
    cfg = write_config(tmp_path / "s.yaml", "scratch-yaml-split", {"adapter": "oracle"}, isolation="split")
    ok(otb("run", "new", run, "--config", cfg))
    ok(otb("run", "register", run))
    p = otb("run", "dispatch", run)
    assert p.returncode == 2 and "split isolation needs OTB_CELLS_DIR" in p.stderr
    bad = write_config(tmp_path / "bad.yaml", "scratch-sideways", {"adapter": "oracle"}, isolation="sideways")
    p = otb("run", "new", tmp_path / "scratch-sideways", "--config", bad)
    assert p.returncode != 0 and "isolation must be in_process or split, not 'sideways'" in p.stderr
    assert otb("cell-worker", "--help").returncode == 0, "the worker verb is registered by the CLI"
    monkeypatch.setenv("OTB_ISOLATION", "split")
    monkeypatch.setenv("OTB_CELLS_DIR", str(workers.cells))

    run = tmp_path / "scratch-missing"
    cfg = one_stack_config(tmp_path, "missing", stacks_with_server(tmp_path, tmp_path / "missing" / "server.py"))
    ok(otb("run", "new", run, "--config", cfg))
    ok(otb("run", "register", run))
    t0 = time.monotonic()
    p = otb("run", "dispatch", run)
    assert p.returncode == 2 and "fixture-stdio: its MCP server failed before any cell ran" in p.stderr
    assert time.monotonic() - t0 < 60
    assert not (run / "cells").exists() or not any((run / "cells").iterdir())
    assert [d.name for d in workers.cells.iterdir() if (d / JOB).exists()] == [], "a probe job left behind"


SLOW_CLAUDE = """#!{python}
import json, os, sys, time
if "--version" in sys.argv:
    print("{version} (Claude Code)")
    sys.exit(0)
emit = lambda e: print(json.dumps(e, separators=(",", ":")), flush=True)
emit({{"type": "system", "subtype": "init", "cwd": os.getcwd(), "session_id": "fake",
      "claude_code_version": "{version}", "model": sys.argv[sys.argv.index("--model") + 1],
      "permissionMode": "default", "tools": ["mcp__telemetry__submit_answer"],
      "mcp_servers": [{{"name": "telemetry", "status": "connected"}}], "plugins": [], "memory_paths": {{}},
      "skills": []}})
time.sleep(2)
emit({{"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "usage": {{}}}})
"""


@pytest.mark.parametrize(("parallel", "most"), [(1, 1), (3, 3)], ids=["one-at-a-time", "three-in-flight"])
def test_split_cells_run_parallel_per_stack_in_one_seeded_order(
    tmp_path: Path, env: dict, monkeypatch: pytest.MonkeyPatch, parallel: int, most: int
) -> None:
    version = "2.1.295"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(SLOW_CLAUDE.format(python=sys.executable, version=version))
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    agent = {"adapter": "claude-code", "model": "claude-haiku-4-5-20251001", "cli_version": version}
    cfg = write_config(
        tmp_path / "c.yaml", "scratch-par", agent, k=2, concurrency="per_stack", parallel=parallel, order_seed=7
    )
    w = Workers(tmp_path / "w", pod_envs(tmp_path, env))
    run = tmp_path / "scratch-par"
    try:
        dispatch_split(monkeypatch, w)
        ok(otb("run", "new", run, "--config", cfg))
        ok(otb("run", "register", run))
        ok(otb("run", "dispatch", run))
    finally:
        w.stop()
    ran = {c: m for c, m in metas(run).items() if m["status"] == "done"}
    by_stack: dict[str, list[dict]] = {}
    for m in ran.values():
        by_stack.setdefault(m["cell"].split("--")[0], []).append(m)
    assert sorted(by_stack) == ["fixture-http", "fixture-stdio"] and all(m["valid"] for m in ran.values())
    assert all(not (run / "cells" / c / "attempts").exists() for c in ran), "a cell ran twice"
    for c, m in ran.items():  # meta's started_utc is whole seconds; the agent side's status has the float
        m["t0"] = json.loads((run / "cells" / c / "agent.status.json").read_text())["started"]
    for stack, ms in by_stack.items():
        edges = sorted([(m["t0"] + m["wall_s"], -1) for m in ms] + [(m["t0"], 1) for m in ms])
        live = peak = 0
        for _, step in edges:
            live += step
            peak = max(peak, live)
        assert peak == most, f"{stack}: {peak} cells in flight at once, want {most}"
    orders = {
        stack: [m["cell"].split("--", 1)[1].rsplit("--", 1)[0] for m in sorted(ms, key=lambda m: m["t0"])]
        for stack, ms in by_stack.items()
    }
    if parallel == 1:
        both = set(orders["fixture-stdio"]) & set(orders["fixture-http"])  # the gate excludes different instances
        a, b = ([i for i in orders[s] if i in both] for s in ("fixture-stdio", "fixture-http"))
        assert a == b, "the two stacks ran the instances in different orders"
        first_rep = a[: len(a) // 2]
        assert first_rep != sorted(first_rep), "order_seed left the instances sorted"


# SLOW_CLAUDE that also appends its --model to a launch log, one line per agent the worker started.
LOGGED_CLAUDE = """#!{python}
import json, os, sys, time
if "--version" in sys.argv:
    print("{version} (Claude Code)")
    sys.exit(0)
model = sys.argv[sys.argv.index("--model") + 1]
with open({launches!r}, "a") as f:
    f.write(model + "\\n")
emit = lambda e: print(json.dumps(e, separators=(",", ":")), flush=True)
emit({{"type": "system", "subtype": "init", "cwd": os.getcwd(), "session_id": "fake",
      "claude_code_version": "{version}", "model": model, "permissionMode": "default",
      "tools": ["mcp__telemetry__submit_answer"], "mcp_servers": [{{"name": "telemetry", "status": "connected"}}],
      "plugins": [], "memory_paths": {{}}, "skills": []}})
time.sleep(2)
emit({{"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "usage": {{}}}})
"""
SHORT_BUDGET = {"calls": 30, "call_timeout_s": 60, "wall_s": 20, "usd": 0.5}


def logged_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launches = tmp_path / "launches.txt"
    fake = bin_dir / "claude"
    fake.write_text(LOGGED_CLAUDE.format(python=sys.executable, version=version, launches=str(launches)))
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return launches


def spans(run: Path) -> dict[str, tuple[float, float]]:
    """Each done cell's agent interval, from the agent side's status (meta's started_utc is whole seconds)."""
    out = {}
    for c, m in metas(run).items():
        if m["status"] == "done":
            st = json.loads((run / "cells" / c / "agent.status.json").read_text())
            out[c] = (st["started"], st["started"] + st["wall_s"])
    return out


def test_two_runs_dispatching_the_same_cells_at_once_keep_their_own(
    tmp_path: Path, env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    version = "2.1.295"
    launches = logged_claude(tmp_path, monkeypatch, version)
    models = {"scratch-twin-a": "claude-haiku-4-5-20251001", "scratch-twin-b": "claude-sonnet-5-5"}
    w = Workers(tmp_path / "w", pod_envs(tmp_path, env))
    try:
        dispatch_split(monkeypatch, w)
        for rid, model in models.items():
            agent = {"adapter": "claude-code", "model": model, "cli_version": version}
            ok(
                otb(
                    "run",
                    "new",
                    tmp_path / rid,
                    "--config",
                    write_config(tmp_path / f"{rid}.yaml", rid, agent, budget=SHORT_BUDGET),
                )
            )
            ok(otb("run", "register", tmp_path / rid))
        procs = {
            rid: subprocess.Popen(
                [sys.executable, "-m", "otb.cli", "run", "dispatch", tmp_path / rid],
                cwd=REPO,
                env=os.environ.copy(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            for rid in models
        }
        out = {rid: p.communicate(timeout=600)[0] for rid, p in procs.items()}
    finally:
        w.stop()
    assert {rid: p.returncode for rid, p in procs.items()} == dict.fromkeys(models, 0), out
    ran = {rid: {c: m for c, m in metas(tmp_path / rid).items() if m["status"] == "done"} for rid in models}
    assert sorted(ran["scratch-twin-a"]) == sorted(ran["scratch-twin-b"]) and len(ran["scratch-twin-a"]) == 6
    for rid, cells in ran.items():
        assert {c: (m["valid"], m["violations"]) for c, m in cells.items()} == dict.fromkeys(cells, (True, [])), rid
        assert {m["model"] for m in cells.values()} == {models[rid]}, f"{rid} collected another run's agent"
        assert not [c for c in cells if (tmp_path / rid / "cells" / c / "attempts").exists()], f"{rid} reran a cell"
    assert sorted(launches.read_text().split()) == sorted(
        [models["scratch-twin-a"]] * 6 + [models["scratch-twin-b"]] * 6
    )
    a, b = spans(tmp_path / "scratch-twin-a"), spans(tmp_path / "scratch-twin-b")
    together = [c for c in a if a[c][0] < b[c][1] and b[c][0] < a[c][1]]
    assert together, "no cell id was in flight in both runs at once, so nothing could collide"


def test_a_cell_whose_shared_directory_vanishes_is_rerun(
    tmp_path: Path, env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    version = "2.1.295"
    launches = logged_claude(tmp_path, monkeypatch, version)
    agent = {"adapter": "claude-code", "model": "claude-haiku-4-5-20251001", "cli_version": version}
    cfg = write_config(tmp_path / "v.yaml", "scratch-vanish", agent, stacks=("fixture-stdio",), budget=SHORT_BUDGET)
    raw = yaml.safe_load(cfg.read_text())
    cfg.write_text(yaml.safe_dump({**raw, "instances": ["fx-l1-count--a1"]}, sort_keys=False))
    cell = "fixture-stdio--fx-l1-count--a1--r1"
    w = Workers(tmp_path / "w", pod_envs(tmp_path, env))
    removed: list[str] = []

    def remove_first_handover() -> None:
        deadline = time.time() + 120
        while not removed and time.time() < deadline:
            for d in w.cells.iterdir() if w.cells.is_dir() else ():
                transcript = d / "transcript.jsonl"
                if d.name.endswith(cell) and transcript.exists() and transcript.stat().st_size:
                    shutil.rmtree(d)
                    removed.append(d.name)
                    return
            time.sleep(0.05)

    janitor = threading.Thread(target=remove_first_handover)
    run = tmp_path / "scratch-vanish"
    try:
        dispatch_split(monkeypatch, w)
        ok(otb("run", "new", run, "--config", cfg))
        ok(otb("run", "register", run))
        janitor.start()
        t0 = time.monotonic()
        p = otb("run", "dispatch", run)
        took = time.monotonic() - t0
        janitor.join()
    finally:
        w.stop()
    assert removed, "the cell's shared directory was never removed"
    assert p.returncode == 0, p.stdout + p.stderr
    lost = json.loads((run / "cells" / cell / "attempts" / "1" / "meta.json").read_text())
    assert not lost["valid"] and any("vanished" in v for v in lost["violations"]), lost["violations"]
    meta = metas(run)[cell]
    assert (meta["valid"], meta["violations"]) == (True, [])
    assert len(launches.read_text().split()) == 2, "the vanished attempt was not rerun once"
    assert took < 60, f"dispatch waited {took:.0f} s on a directory that was gone"


# Refuses every attempt with Claude Code's usage-limit events until a reset fixed 3 s after the first refusal,
# then answers. The reset is whole seconds, as the CLI's rate_limit_info.resetsAt is.
LIMITED_CLAUDE = """#!{python}
import json, math, os, sys, time
if "--version" in sys.argv:
    print("{version} (Claude Code)")
    sys.exit(0)
emit = lambda e: print(json.dumps(e, separators=(",", ":")), flush=True)
emit({{"type": "system", "subtype": "init", "cwd": os.getcwd(), "session_id": "fake",
      "claude_code_version": "{version}", "model": sys.argv[sys.argv.index("--model") + 1],
      "permissionMode": "default", "tools": ["mcp__telemetry__submit_answer"],
      "mcp_servers": [{{"name": "telemetry", "status": "connected"}}], "plugins": [], "memory_paths": {{}},
      "skills": []}})
try:
    fd = os.open({reset!r}, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(fd, str(math.ceil(time.time() + 3)).encode())
    os.close(fd)
except FileExistsError:
    pass
reset = int(open({reset!r}).read() or 0) or math.ceil(time.time() + 3)
if time.time() < reset:
    emit({{"type": "rate_limit_event", "rate_limit_info": {{"status": "rejected", "resetsAt": reset,
          "rateLimitType": "five_hour"}}}})
    emit({{"type": "result", "subtype": "success", "is_error": True, "num_turns": 1, "api_error_status": 429,
          "terminal_reason": "api_error", "result": "You've hit your limit", "usage": {{}}}})
    sys.exit(1)
emit({{"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "usage": {{}}}})
"""


def test_a_usage_limit_pauses_every_stack_until_it_resets_and_costs_no_retry(
    tmp_path: Path, env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    version = "2.1.295"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    reset_file = tmp_path / "reset"
    fake = LIMITED_CLAUDE.format(python=sys.executable, version=version, reset=str(reset_file))
    (bin_dir / "claude").write_text(fake)
    (bin_dir / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    agent = {"adapter": "claude-code", "model": "claude-haiku-4-5-20251001", "cli_version": version}
    cfg = write_config(
        tmp_path / "c.yaml", "scratch-limit", agent, concurrency="per_stack", parallel=2, limit_margin_s=0.5
    )
    w = Workers(tmp_path / "w", pod_envs(tmp_path, env))
    run = tmp_path / "scratch-limit"
    try:
        dispatch_split(monkeypatch, w)
        run_all(run, cfg)
    finally:
        w.stop()
    reset = int(reset_file.read_text())
    ran = {c: m for c, m in metas(run).items() if m["status"] == "done"}
    assert {m["cell"].split("--")[0] for m in ran.values()} == {"fixture-http", "fixture-stdio"}
    assert all(m["valid"] and m["usage_limit"] is None for m in ran.values()), "a cell ended invalid or limited"
    assert all(not (run / "cells" / c / "attempts").exists() for c in ran), "a refusal used up an invalid retry"
    refused = sorted((run / "cells").glob("*/refused/*/meta.json"))
    assert refused, "no attempt was refused, so the limit was never met"
    for p in refused:
        m = json.loads(p.read_text())
        assert m["usage_limit"] == {"status": 429, "resets_at": reset, "type": "five_hour"}, m["usage_limit"]
        assert not m["valid"] and m["violations"] == ["model API refused the request (429)"]
    starts = {c: json.loads((run / "cells" / c / "agent.status.json").read_text())["started"] for c in ran}
    early = {c: t for c, t in starts.items() if t < reset}
    assert early == {}, f"cells started before the limit reset at {reset}: {early}"
    pauses = read_jsonl(run / "dispatch" / "pauses.jsonl")
    assert [(p["resets_at"], p["type"]) for p in pauses] == [(reset, "five_hour")], pauses
    assert set(graded(run)) == set(metas(run)), "a refused attempt was graded, or a cell was not"
    ok(otb("run", "selfcheck", run))
    keep(run, "run-usage-limit")


def harness_copy(dst: Path, moved: bool) -> Path:
    """otb/ alone, as the runner pod mounts it into a worker container (no .git), with one module changed when the
    checkout moved on after the worker started."""
    shutil.copytree(REPO / "otb", dst / "otb", ignore=shutil.ignore_patterns("__pycache__"))
    if moved:
        adapter = dst / "otb" / "run" / "agents" / "claude_code.py"
        adapter.write_text(adapter.read_text() + "\n# the launch line as of the next commit\n")
    return dst


def heartbeat_only(cells: Path, role: str) -> subprocess.Popen:
    """A worker from before the harness report: it refreshes its heartbeat file and says nothing else."""
    loop = "import pathlib, sys, time\np = pathlib.Path(sys.argv[1])\nwhile True:\n    p.touch()\n    time.sleep(0.2)\n"
    return subprocess.Popen([sys.executable, "-c", loop, str(cells / f".{role}.alive")])


@pytest.mark.parametrize("agent_code", ["same", "moved", "unreported"])
def test_dispatch_refuses_a_worker_that_imported_other_harness_code(
    tmp_path: Path, env: dict, monkeypatch: pytest.MonkeyPatch, agent_code: str
) -> None:
    rid = f"scratch-code-{agent_code}"
    cfg = write_config(tmp_path / "c.yaml", rid, {"adapter": "oracle"}, stacks=("fixture-stdio",))
    raw = yaml.safe_load(cfg.read_text())
    cfg.write_text(yaml.safe_dump({**raw, "instances": ["fx-l1-count--a1"]}, sort_keys=False))
    envs = pod_envs(tmp_path, env)
    code = {}
    if agent_code == "unreported":
        del envs["agent"]
    else:
        code = {"agent": harness_copy(tmp_path / "agent-checkout", moved=agent_code == "moved")}
    w = Workers(tmp_path / "w", envs, code)
    old = heartbeat_only(w.cells, "agent") if agent_code == "unreported" else None
    run = tmp_path / rid
    try:
        dispatch_split(monkeypatch, w)
        ok(otb("run", "new", run, "--config", cfg))
        ok(otb("run", "register", run))
        p = otb("run", "dispatch", run)
    finally:
        w.stop()
        if old is not None:
            old.kill()
            old.wait()
    if agent_code == "same":
        assert p.returncode == 0, p.stdout + p.stderr
        assert metas(run)["fixture-stdio--fx-l1-count--a1--r1"]["valid"]
        return
    reported = "unreported" if agent_code == "unreported" else "[0-9a-f]{12}"
    want = f"the agent worker on \\S+ runs harness source {reported}, not this checkout's [0-9a-f]{{12}}"
    assert p.returncode == 2 and re.search(want, p.stderr), p.stderr
    assert not (run / "cells").exists(), "a cell ran on a worker with other code"
    assert [d.name for d in w.cells.iterdir() if d.is_dir()] == [], "a job was handed to the workers"
