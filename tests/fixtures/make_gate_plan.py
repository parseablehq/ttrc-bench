"""Write tests/fixtures/gate-plan-mini.json: the gate plan over corpus-mini with a fixed seed, three sampled minutes,
four sampled traces and, for b1-lgtm, the traces to fetch by id under a 10 s live window (corpus-mini has none
over B1's real 60 s). tests/e2e/test_gate_plan.py rebuilds the plan and compares, so the sampled part of a plan
and the by-id fetch list cannot move without a deliberate refresh of this file.

  uv run python tests/fixtures/make_gate_plan.py
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import replace
from pathlib import Path

import yaml

from otb.archive import Archive
from otb.gate import Plan, frame_dues, long_traces, make_plan, trace_windows
from otb.ref.build import build
from otb.ref.store import ReferenceStore

MINI = Path(__file__).parent / "corpus-mini"
OUT = Path(__file__).parent / "gate-plan-mini.json"
SEED = 20261002
LIVE_S = 10.0
STACK = "b1-lgtm"


def plan_mini(corpus: Path) -> Plan:
    """The plan of a built copy of corpus-mini, as build_plan makes it, under this file's parameters."""
    archive = Archive(corpus)
    store = ReferenceStore.open(corpus)
    schedule = yaml.safe_load((corpus / "schedule.yaml").read_text()) if (corpus / "schedule.yaml").exists() else {}
    scope = archive.manifest.window
    assert scope is not None
    plan = make_plan(
        store,
        schedule,
        scope,
        seed=SEED,
        session="corpus-mini",
        corpus_id=archive.manifest.corpus_id,
        minute_windows=3,
        trace_samples=4,
    )
    found = long_traces(store, frame_dues(archive), LIVE_S, trace_windows(plan))
    return replace(plan, fetch_by_id={STACK: {"live_window_s": LIVE_S, "traces": found}})


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        corpus = Path(tmp) / "corpus-mini"
        shutil.copytree(MINI, corpus)
        build(corpus, workers=2)
        plan = plan_mini(corpus)
    OUT.write_bytes(plan.to_json())
    print(f"{OUT}: {len(plan.points)} points, {len(plan.by_id_traces(STACK))} traces by id")


if __name__ == "__main__":
    main()
