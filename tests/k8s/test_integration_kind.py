"""The Kubernetes integration run on kind (tests/k8s/integration_kind.py), judged against the acceptance
criteria of the integration brief. Two tests: the live one (OTB_K8S_TESTS=1 and OTB_K8S_CONTEXT=kind-<cluster>,
hours; the runner pod deployed first as deploy/k8s/runner/README.md says) runs every phase and writes
tests/k8s/artifacts/integration-kind.json; the offline one judges the committed file, so the verdicts are
reproducible from the evidence alone.

A1  the corpus is sealed and holds demo eu1 traces, logs and metrics including kubeletstats container metrics,
    and background telemetry under cloud.region=bg; the realised schedule shows the fault and the decoy.
A2  the gate passes on each stack, or every failure is explained (a backend fact, not a harness bug): here, a
    failure counts as explained when it is a probe error (the backend did not answer) and the record shows the
    backend's container OOM-killed or restarted during the gate.
A3  the oracle's cells are correct for every task whose instance is not excluded; selfcheck all pass or skip.
A4  no truth value changes when the background is removed from the corpus.
A5  doctor passes on the gke profile render; every pod Guaranteed; equal per-stack sums.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
EVIDENCE = REPO / "tests/k8s/artifacts/integration-kind.json"
SCRIPT = REPO / "tests/k8s/integration_kind.py"
PHASES = ["runner", "bootstrap", "demo", "capture", "truth", "stack b4", "stack b3", "stack b1", "scoping", "doctor"]


@pytest.mark.skipif(
    os.environ.get("OTB_K8S_TESTS") != "1" or not os.environ.get("OTB_K8S_CONTEXT", "").startswith("kind-"),
    reason="needs OTB_K8S_TESTS=1 and OTB_K8S_CONTEXT=kind-<cluster> with the runner pod deployed",
)
def test_run_every_phase() -> None:
    for phase in PHASES:
        p = subprocess.run([sys.executable, str(SCRIPT), *phase.split()], cwd=REPO, timeout=4 * 3600)
        assert p.returncode == 0, phase


def judge(doc: dict) -> dict[str, dict]:
    """Each criterion with its verdict and the numbers it rests on; the live run and a reader get the same."""
    capture, truth = doc.get("capture", {}), doc.get("truth", {})
    corpus = truth.get("corpus", {})
    regions = {r: n for r, n in corpus.get("regions", [])}
    entries = truth.get("schedule") or capture.get("schedule", [])
    kinds = sorted((e.get("kind"), e.get("decoy_type") or e.get("flag")) for e in entries)
    names = corpus.get("container_metric_names", [])
    a1 = {
        "sealed": capture.get("sealed") is True,
        "regions": regions,
        "rows": corpus.get("rows"),
        "schedule": kinds,
        "container_metric_names": names,
    }
    a1["verdict"] = all(
        [
            a1["sealed"],
            regions.get("eu1", 0) > 0 and regions.get("bg", 0) > 0,
            all(corpus.get("rows", {}).get(t, 0) > 0 for t in ("spans", "logs", "points")),
            any(n.startswith("container.") for n in names),
            ("fault", "paymentFailure") in kinds and ("decoy", "benign_restart") in kinds,
        ]
    )
    stacks = {k[len("stack-") :]: v for k, v in doc.items() if re.fullmatch(r"stack-b\d", k)}

    def explained(v: dict) -> bool:
        not_pass = (v.get("gate_summary") or {}).get("not_pass") or []
        # the backend restarted (OOM, liveness) or stopped answering its readiness probe during the gate: it was
        # unreachable or too slow at this budget, which a probe error records and a data error would not
        did_not_answer = "OOMKilled" in json.dumps(v.get("prometheus_watch") or {}) or any(
            "Liveness" in e or "OOM" in e or "Readiness probe failed" in e for e in v.get("backend_events") or []
        )
        return (
            bool(not_pass)
            and all(str(c.get("reason", "")).startswith("probe error") for c in not_pass)
            and did_not_answer
        )

    a2 = {
        s: {
            "gate_exit": v.get("gate_exit"),
            "counts": (v.get("gate_summary") or {}).get("counts"),
            "explained_by_backend_restart": explained(v),
        }
        for s, v in stacks.items()
    }
    a2["verdict"] = bool(stacks) and all(
        v.get("gate_exit") == 0
        and (((v.get("gate_summary") or {}).get("counts") or {}).get("fail", 1) == 0 or explained(v))
        for v in stacks.values()
    )
    before = doc.get("stack-b1-before-no-liveness")
    if before:
        a2["b1_before_no_liveness"] = {
            "counts": (before.get("gate_summary") or {}).get("counts"),
            "outcomes": before.get("outcome_counts"),
            "prometheus_restarts": (before.get("prometheus_watch") or {}).get("restarts"),
        }
    a3 = {
        s: {
            "outcomes": v.get("outcome_counts"),
            "selfcheck": v.get("selfcheck", {}).get("pass"),
            "cells": v.get("cells"),
        }
        for s, v in stacks.items()
    }
    a3["verdict"] = bool(stacks) and all(
        v.get("selfcheck", {}).get("pass") is True
        and set(v.get("outcome_counts", {})) <= {"correct", "excluded"}
        and v.get("outcome_counts", {}).get("correct", 0) > 0
        and all(
            c.get("valid") and c.get("isolation") == "split"
            for c in v.get("cells", {}).values()
            if c.get("status") == "done"
        )
        for v in stacks.values()
    )
    scoping = doc.get("scoping", {})
    a4 = dict(scoping)
    a4["verdict"] = (
        bool(scoping) and scoping.get("instances_whose_truth_changed") == [] and scoping.get("frames_dropped_bg", 0) > 0
    )
    doctor = doc.get("doctor", {})
    qos = doctor.get("qos_now", {})
    a5 = {
        "gke_exit": doctor.get("gke_exit"),
        "equal_backend_sums": doctor.get("equal_backend_sums"),
        "equal_ingest_sums": doctor.get("equal_ingest_sums"),
        "qos_now": {k: len(v) for k, v in qos.items()},
        "qos_after_up": {s: {k: len(v) for k, v in v.get("qos_after_up", {}).items()} for s, v in stacks.items()},
    }
    not_guaranteed = {k: v for k, v in qos.items() if k != "Guaranteed"} | {
        f"{s}:{k}": v for s, st in stacks.items() for k, v in st.get("qos_after_up", {}).items() if k != "Guaranteed"
    }
    a5["not_guaranteed"] = not_guaranteed
    a5["verdict"] = doctor.get("gke_exit") == 0 and doctor.get("equal_backend_sums") is True and not not_guaranteed
    return {"A1": a1, "A2": a2, "A3": a3, "A4": a4, "A5": a5}


@pytest.mark.skipif(not EVIDENCE.exists(), reason="no committed integration evidence")
def test_committed_evidence_meets_the_acceptance_criteria() -> None:
    doc = json.loads(EVIDENCE.read_text())
    verdicts = judge(doc)
    failed = {k: v for k, v in verdicts.items() if not v["verdict"]}
    assert failed == {}, json.dumps(failed, indent=1)[:4000]
