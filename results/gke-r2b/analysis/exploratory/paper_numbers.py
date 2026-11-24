"""Numbers of run 2b that the paper states and the registered analysis does not print.

    uv run python results/gke-r2b/analysis/exploratory/paper_numbers.py

Its output is paper_numbers.txt, beside this file.

Part 1 gives the three-tool Parseable configuration against ClickHouse and Grafana LGTM as billed USD per solved
question, with the registered analysis code, bootstrap seed and resample count. Part 2 gives how much of a gap in
input tokens the tool list explains: the difference in first-round input tokens, which is the tool list, times the
rounds of the configuration with the shorter list, over the difference in input tokens per attempt. None of these
is in the pre-registration.
"""

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "k8s/gke"))
import r2_analysis as r2  # noqa: E402
import r2b_analysis  # noqa: E402, F401  sets r2.MODELS for run 2b

from otb.report import contrast  # noqa: E402
from otb.run.config import Contrast, load_config  # noqa: E402

res = REPO / "results/gke-r2b"
cfg = load_config(res / "r2b-agent").analysis
recs = [r for m in r2.MODELS for r in r2.load(res, m, set())]
print("billed USD per solved question, ratio with its 95% interval")
for m in r2.MODELS:
    for den in ("b3-clickhouse", "b1-lgtm"):
        c = Contrast(f"{m}:tuned-vs-{den}", "usd_per_solved", f"{m}:{r2.TUNED}", f"{m}:{den}", r2.ALL, "lt1")
        g = contrast(c, recs, cfg.bootstrap_seed, cfg.resamples)
        print(c.name, round(g["point"], 3), round(g["lo"], 3), round(g["hi"], 3))


def rows(run: str, stack: str) -> list[dict]:
    with (res / run / "grade/runs.jsonl").open() as fh:
        return [r for r in map(json.loads, fh) if r["stack"] == stack]


def mean(rs: list[dict], f) -> float:
    return sum(map(f, rs)) / len(rs)


def tokens_in(r: dict) -> int:
    u = r["usage"]
    return u["input_tokens"] + u["cache_creation_input_tokens"] + u["cache_read_input_tokens"]


def first(r: dict) -> int:
    return r["first_turn_input_tokens"]


def share(name: str, long: list[dict], short: list[dict]) -> None:
    gap = mean(long, tokens_in) - mean(short, tokens_in)
    list_tokens = mean(long, first) - mean(short, first)
    list_bytes = mean(long, lambda r: r["manifest_ctx_bytes"]) - mean(short, lambda r: r["manifest_ctx_bytes"])
    rounds = mean(short, lambda r: r["rounds"])
    print(
        name,
        f"input-token gap {gap:.0f}",
        f"tool list larger by {list_bytes:.0f} bytes, {list_tokens:.0f} tokens",
        f"rounds {rounds:.2f}",
        f"share of the gap {100 * list_tokens * rounds / gap:.1f}%",
    )


print("share of an input-token gap that the tool list explains")
for m, base in (("sonnet", "r2b-agent"), ("opus", "r2b-opus")):
    tuned = rows(f"{base}-b4-tuned", "b4-parseable")
    share(f"{m}:lgtm-vs-tuned", rows(base, "b1-lgtm"), tuned)
    share(f"{m}:shipped-vs-tuned", rows(base, "b4-parseable"), tuned)
