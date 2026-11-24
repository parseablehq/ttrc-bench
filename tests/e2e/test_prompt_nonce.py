"""Every run starts cold on the model's prompt cache: the system prompt opens with a nonce drawn once per run.

Claude's prompt cache is shared across sessions for an hour, so a run that sends the prompts an earlier run sent
within the hour pays far less (gke-24h: ClickHouse billed $1.11 warm and $1.93 cold on the same questions). Two
runs of one config dispatch the claude-code adapter here against a fake CLI that logs the prompts it was given.

Ways this could fail, listed before the code:
1. two runs of the same questions send the same system prompt, so the second reads the first's cache;
2. repeats of one instance inside a run get different system prompts, losing the cache they shared before;
3. a resumed dispatch draws a new nonce, so the cells of one run differ;
4. the nonce reaches the graded content (the question) or prompt_sha256, so one question hashes differently in
   two runs, or the run does not record its nonce;
5. pre-registration or a dispatch that resumes after it refuses the run because of the nonce;
6. the report prints billed cost without the uncached cost beside it.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from conftest import ok, otb, write_config

from otb.tasks.suite import read_jsonl

VERSION = "2.1.295"
MODEL = "claude-haiku-4-5-20251001"
USAGE = {"input_tokens": 1000, "cache_read_input_tokens": 9000, "output_tokens": 100}
PROMPT_LOGGING_CLAUDE = """#!{python}
import json, os, sys
if "--version" in sys.argv:
    print("{version} (Claude Code)")
    sys.exit(0)
arg = lambda name: sys.argv[sys.argv.index(name) + 1]
with open({log!r}, "a") as f:
    f.write(json.dumps({{"system": arg("--system-prompt"), "user": arg("-p")}}) + "\\n")
emit = lambda e: print(json.dumps(e, separators=(",", ":")), flush=True)
emit({{"type": "system", "subtype": "init", "cwd": os.getcwd(), "session_id": "fake",
      "claude_code_version": "{version}", "model": arg("--model"), "permissionMode": "default",
      "tools": ["mcp__telemetry__submit_answer"], "mcp_servers": [{{"name": "telemetry", "status": "connected"}}],
      "plugins": [], "memory_paths": {{}}, "skills": []}})
emit({{"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "usage": {usage}}})
"""


def test_each_run_has_its_own_nonce_and_repeats_share_it(
    tmp_path: Path, env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "prompts.jsonl"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(PROMPT_LOGGING_CLAUDE.format(python=sys.executable, version=VERSION, log=str(log), usage=USAGE))
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    agent = {"adapter": "claude-code", "model": MODEL, "cli_version": VERSION}
    runs = {rid: tmp_path / rid for rid in ("scratch-cold-a", "scratch-cold-b")}
    for rid, run in runs.items():
        cfg = write_config(tmp_path / f"{rid}.yaml", rid, agent, stacks=("fixture-stdio",), k=2)
        ok(otb("run", "new", run, "--config", cfg))
        ok(otb("run", "register", run))
    a, b = runs.values()
    ok(otb("run", "dispatch", a, "--only=--r1"))
    ok(otb("run", "dispatch", a))
    ok(otb("run", "dispatch", b))

    def cells(run: Path) -> dict[str, dict]:
        out = {}
        for d in sorted((run / "cells").iterdir()):
            meta = json.loads((d / "meta.json").read_text())
            if meta["status"] == "done":
                out[d.name] = {"meta": meta, "prompt": json.loads((d / "prompt.json").read_text())}
        return out

    nonce = {run: json.loads((run / "dispatch" / "stamp.json").read_text())["prompt_nonce"] for run in (a, b)}
    assert nonce[a] != nonce[b] and all(len(n) == 16 for n in nonce.values()), nonce
    ca, cb = cells(a), cells(b)
    assert sorted(ca) == sorted(cb) and len(ca) == 8, sorted(ca)
    for run, cs in ((a, ca), (b, cb)):
        assert {c["meta"]["prompt_nonce"] for c in cs.values()} == {nonce[run]}, "the cells of one run differ"
    sent = read_jsonl(log)
    assert len(sent) == 16
    for run, cs in ((a, ca), (b, cb)):
        for c in cs.values():
            want = f"Benchmark run {nonce[run]}.\n\n{c['prompt']['system']}"
            assert {"system": want, "user": c["prompt"]["user"]} in sent, "the agent got another system prompt"
            assert nonce[run] not in c["prompt"]["user"] and nonce[run] not in c["prompt"]["system"]
    by_question = {}
    for cs in (ca, cb):
        for cell, c in cs.items():
            by_question.setdefault(cell.rsplit("--", 1)[0], set()).add(c["meta"]["prompt_sha256"])
    assert all(len(h) == 1 for h in by_question.values()), "one question hashes differently across repeats or runs"
    distinct = {(s["system"], s["user"]) for s in sent}
    assert len(distinct) == 8, "two runs shared a prompt, or two repeats in one run did not"

    ok(otb("run", "grade", a))
    report = ok(otb("run", "report", a)).stdout
    assert (
        "| Level | k | s | Stack | instances | runs | success [95% CI] | credit | $ per run | uncached $ per run |"
        in report
    )
    assert "| Stack | runs | $ total | uncached $ total | $ per run | uncached $ per run |" in report
    # 1000 input, 9000 cache-read and 100 output tokens at Haiku 4.5's list prices: billed $0.0024, uncached $0.0105
    assert "| L1 | 1 | 1 | F1 fixture (stdio) | 2 | 4 | 0.00 [0.00, 0.00] | 0.0000 | 0.002400 | 0.010500 |" in report
    assert "| F1 fixture (stdio) | 8 | 0.019200 | 0.084000 | 0.002400 | 0.010500 |" in report, report
