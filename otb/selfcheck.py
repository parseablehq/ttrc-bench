"""Offline checks of a dispatched and graded run. Needs no live stack and writes nothing into the run's outputs:
the regrade goes to a temporary directory and only runs/X/selfcheck.json is written.

Each check names what it would see if the harness were broken, so a pass is evidence, not absence of noise.
Statuses: pass, fail, skip (the check cannot run yet; never counted as a pass).
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path

from otb.grade import RAN, grade_into
from otb.report import build
from otb.run.config import REPO, RunRefused, changed_since_prereg, load_config, load_stacks
from otb.run.dispatch import plan_cells
from otb.stacks.secrets import read_env
from otb.tasks.suite import load_instances, read_jsonl

Check = dict


def rejudge_gate(inputs: Path) -> bool | None:
    """Re-derive the frozen gate verdict from the gate evidence copied into inputs/gate/ (plan, reference values,
    raw probe responses, ledger reports) by replaying every probe offline. None when the run's session carries no
    probe evidence (fixture sessions)."""
    if not (inputs / "gate" / "plan.json").exists():
        return None
    from otb.gate import rejudge

    got, _ = rejudge(inputs)
    return got == (inputs / "gate.json").read_bytes()


def regrade_byte_identical(run_dir: Path) -> Check:
    with tempfile.TemporaryDirectory() as tmp:
        g = Path(tmp) / "grade"
        grade_into(run_dir, g)
        files = {f"grade/{n}": (g / n).read_bytes() for n in ("runs.jsonl", "summary.json")}
        files |= {f"report/{k}": v.encode() for k, v in build(run_dir, g).items()}
    differ = sorted(
        rel for rel, data in files.items() if not (run_dir / rel).exists() or (run_dir / rel).read_bytes() != data
    )
    return {"status": "fail" if differ else "pass", "compared": sorted(files), "differ": differ}


def inputs_match_prereg(run_dir: Path) -> Check:
    try:
        changed = changed_since_prereg(run_dir)
    except RunRefused as e:
        return {"status": "fail", "reason": str(e)}
    frozen = [k for k in changed if not k.startswith("repo/")]
    return {
        "status": "fail" if frozen else "pass",
        "inputs_changed": frozen,
        "code_changed_since_prereg": [k for k in changed if k.startswith("repo/")],
    }


def every_cell_isolated(recs: list[dict]) -> Check:
    bad = sorted(r["cell"] for r in recs if r["outcome"] == "invalid")
    return {"status": "fail" if bad else "pass", "invalid": bad}


def ctx_bytes_match_transcript(recs: list[dict]) -> Check:
    ran = [r for r in recs if r["outcome"] in RAN]
    bad = sorted(r["cell"] for r in ran if not r["ctx_matches_transcript"])
    return {"status": "fail" if bad else "pass", "cells": len(ran), "mismatches": bad}


def one_commit_model_client(run_dir: Path, recs: list[dict]) -> Check:
    cfg = load_config(run_dir)
    ran = [r for r in recs if r["outcome"] in RAN]
    commits = sorted({r["harness_commit"] for r in ran})
    models = sorted({r["model"] for r in ran}, key=str)
    clients = sorted({r["client_version"] for r in ran}, key=str)
    dirty = sorted(r["cell"] for r in ran if r["harness_dirty"])
    ok = len(commits) <= 1 and models in ([], [cfg.agent.model]) and clients in ([], [cfg.agent.cli_version])
    ok = ok and (cfg.scratch or not dirty)
    return {
        "status": "pass" if ok else "fail",
        "commits": commits,
        "models": models,
        "client_versions": clients,
        "dirty_cells": dirty,
    }


def cell_metas(run_dir: Path) -> list[dict]:
    """meta.json of every cell that ran and counts: done, valid, not excluded."""
    metas = [json.loads(p.read_text()) for p in sorted((run_dir / "cells").glob("*/meta.json"))]
    return [m for m in metas if m.get("status") == "done" and m.get("valid")]


def one_manifest_per_stack(run_dir: Path) -> Check:
    """Every valid cell of a stack saw one and the same vendor tools/list (the proxy's sha256 of the full result):
    a vendor server that lists different tools across cells, or a cell without a recorded manifest, fails. Runs
    dispatched before the hash was recorded cannot be checked."""
    metas = cell_metas(run_dir)
    if not any(m.get("manifest_sha256") for m in metas):
        return {"status": "skip", "reason": "no cell recorded a manifest hash (dispatched before it was recorded)"}
    by_stack: dict[str, set[str]] = {}
    unhashed = []
    for m in metas:
        if m.get("manifest_sha256"):
            by_stack.setdefault(m["stack"], set()).add(m["manifest_sha256"])
        else:
            unhashed.append(m["cell"])
    hashes = {s: sorted(h) for s, h in sorted(by_stack.items())}
    divergent = [s for s, h in hashes.items() if len(h) != 1]
    return {
        "status": "fail" if divergent or unhashed else "pass",
        "manifests": hashes,
        "divergent_stacks": divergent,
        "cells_without_manifest": unhashed,
    }


def one_client_skills_set(run_dir: Path) -> Check:
    """Every valid cell's client listed the same skills. A CLI ships skills of its own, so the set is recorded and
    compared, not required empty; a cell whose client loaded extra skills from somewhere fails the run."""
    metas = [m for m in cell_metas(run_dir) if m.get("client_skills") is not None]
    if not metas:
        return {"status": "skip", "reason": "no cell recorded client skills"}
    sets = sorted({tuple(m["client_skills"]) for m in metas})
    return {"status": "pass" if len(sets) == 1 else "fail", "skills": [list(s) for s in sets]}


def exclusions_follow_gate(run_dir: Path) -> Check:
    cfg = load_config(run_dir)
    inputs = run_dir / "inputs"
    plan = plan_cells(cfg, load_instances(inputs / "instances.jsonl"), json.loads((inputs / "gate.json").read_text()))
    wrong, missing, reworded = [], [], []
    for c in plan:
        meta_path = run_dir / "cells" / c.cell_id / "meta.json"
        if not meta_path.exists():
            missing.append(c.cell_id)
            continue
        meta = json.loads(meta_path.read_text())
        if (meta["status"] == "excluded") != (c.excluded is not None):
            wrong.append(c.cell_id)
        elif meta.get("excluded") != c.excluded:
            reworded.append(c.cell_id)  # same decision; the reason's wording is presentation, not the invariant
    return {
        "status": "fail" if wrong or missing else "pass",
        "planned": len(plan),
        "wrong": wrong,
        "missing": missing,
        "reason_reworded": reworded,
    }


def gate_rejudge_identical(run_dir: Path) -> Check:
    verdict = rejudge_gate(run_dir / "inputs")
    if verdict is None:
        return {"status": "skip", "reason": "the run's session has no recorded gate probes"}
    return {"status": "pass" if verdict else "fail"}


SHORT_SECRET = 16  # shorter values in .otb/secrets are static usernames (admin, otb_agent), not secrets


def no_local_paths_or_secrets(run_dir: Path, secrets_dir: Path = REPO / ".otb" / "secrets") -> Check:
    """Committed cells must be relocatable and must not carry credentials: no home or repo path, no value stored in
    .otb/secrets/*.env, and no value of an MCP credential set in this environment."""
    needles = {"home path": str(Path.home()), "repo path": str(REPO)}
    short = []
    for f in sorted(secrets_dir.glob("*.env")):
        for name, value in read_env(f).items():
            if len(value) >= SHORT_SECRET:
                needles[f"{f.name} {name}"] = value
            else:
                short.append(f"{f.name} {name}")
    for s in load_stacks(run_dir).values():
        needles |= {f"credential {n}": os.environ[n] for n in s.credentials if len(os.environ.get(n, "")) >= 8}
    hits = []
    for p in sorted((run_dir / "cells").rglob("*")):
        if p.is_file():
            text = p.read_text(errors="replace")
            hits += [f"{p.relative_to(run_dir)}: {what}" for what, n in needles.items() if n in text]
    return {
        "status": "fail" if hits else "pass",
        "hits": hits,
        "values_scanned": len(needles),
        "short_values_not_scanned": short,
    }


def selfcheck(run_dir: Path) -> dict:
    graded = (run_dir / "grade" / "runs.jsonl").exists()
    recs = read_jsonl(run_dir / "grade" / "runs.jsonl") if graded else []
    checks: dict[str, Callable[[], Check]] = {
        "regrade_byte_identical": lambda: regrade_byte_identical(run_dir),
        "inputs_match_prereg": lambda: inputs_match_prereg(run_dir),
        "every_cell_isolated": lambda: every_cell_isolated(recs),
        "ctx_bytes_match_transcript": lambda: ctx_bytes_match_transcript(recs),
        "one_commit_model_client": lambda: one_commit_model_client(run_dir, recs),
        "exclusions_follow_gate": lambda: exclusions_follow_gate(run_dir),
        "one_manifest_per_stack": lambda: one_manifest_per_stack(run_dir),
        "one_client_skills_set": lambda: one_client_skills_set(run_dir),
        "gate_rejudge_identical": lambda: gate_rejudge_identical(run_dir),
        "no_local_paths_or_secrets": lambda: no_local_paths_or_secrets(run_dir),
    }
    graded_only = {
        "regrade_byte_identical",
        "every_cell_isolated",
        "ctx_bytes_match_transcript",
        "one_commit_model_client",
    }
    results = {
        name: ({"status": "fail", "reason": "run is not graded"} if name in graded_only and not graded else fn())
        for name, fn in checks.items()
    }
    out = {"pass": all(c["status"] != "fail" for c in results.values()), "checks": results}
    (run_dir / "selfcheck.json").write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    return out
