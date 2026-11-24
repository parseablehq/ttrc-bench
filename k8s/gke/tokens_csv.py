"""One CSV row per graded question of the GKE pilot's agent runs, for the token-utilisation sheet.

    OTB_K8S_CONTEXT=<gke-context> uv run python k8s/gke/tokens_csv.py gkp-agent-b4 [gkp-agent-b3 ...] > tokens.csv

Reads each run's grade/runs.jsonl from the runner pod, so every number is the grader's, not recomputed here.
`usd` is the API-list-price equivalent from otb/cost.py; a run on a subscription token is not billed per token.
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys

CONTEXT = os.environ["OTB_K8S_CONTEXT"]  # the kube context of the cluster that holds the runner pod
TOKENS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
    "thinking_tokens",
)
COLUMNS = (
    "run",
    "stack",
    "instance_id",
    "level",
    "template_id",
    "control",
    "outcome",
    *TOKENS,
    "total_tokens",
    "usd",
    "calls",
    "rounds",
    "wall_s",
    "ctx_bytes",
    "model",
    "client_version",
)


def graded(run_id: str) -> list[dict]:
    out = subprocess.run(
        ["kubectl", "--context", CONTEXT, "-n", "otb", "exec", "otb-runner-0", "-c", "runner", "--",
         "cat", f"/work/runs/{run_id}/grade/runs.jsonl"],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def main(run_ids: list[str]) -> None:
    w = csv.DictWriter(sys.stdout, COLUMNS, extrasaction="ignore")
    w.writeheader()
    for run_id in run_ids:
        for r in graded(run_id):
            usage = r.get("usage") or {}
            tokens = {k: usage.get(k, 0) for k in TOKENS}
            # thinking tokens are already counted inside output_tokens
            total = sum(v for k, v in tokens.items() if k != "thinking_tokens")
            w.writerow({**r, **tokens, "run": run_id, "total_tokens": total, "usd": round(r.get("usd") or 0, 6)})


if __name__ == "__main__":
    main(sys.argv[1:])
