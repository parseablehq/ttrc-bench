"""The one price table and the one cost formula. Relative cost is a ratio of these dollars, never a separate index.

Keyed by the model id the run pins (agent.model in run.yaml, which the agent's init event must echo). A model
missing from the table fails at `otb run new`, not at grade time. Each entry carries the date its prices were
read and their source.

Source: Anthropic list prices per million tokens from the claude-api skill's model table (cached 2026-06-24),
read 2026-10-01. Cache writes: 5-minute 1.25x input, 1-hour 2x input. Cache reads 0.1x input, except Opus 5.5
whose table lists $0.20 (0.05x). Sonnet 5.5 from platform.claude.com/docs/en/about-claude/pricing, read
2026-10-27: cache reads 0.05x input there too.
"""

from __future__ import annotations

from dataclasses import dataclass

from otb.run.agents.base import Usage


@dataclass(frozen=True, slots=True)
class Price:
    as_of: str
    input: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float
    output: float


PRICES: dict[str, Price] = {
    "claude-opus-5-5": Price("2026-09-29", 4.00, 5.00, 8.00, 0.20, 20.00),
    "claude-haiku-4-5-20251001": Price("2026-10-01", 1.00, 1.25, 2.00, 0.10, 5.00),
    "claude-sonnet-5-5": Price("2026-10-27", 2.00, 2.50, 4.00, 0.10, 10.00),
}


def price(model: str) -> Price:
    if model not in PRICES:
        raise ValueError(
            f"no price for model {model!r}; add a dated entry to otb/cost.py PRICES (known: {sorted(PRICES)})"
        )
    return PRICES[model]


def dollars(usage: Usage, model: str | None) -> float:
    """List-price dollars for one cell's provider usage, with cache discounts. Scripted agents cost nothing."""
    if model is None:
        return 0.0
    p = price(model)
    w1 = usage.cache_creation_1h_input_tokens
    w5 = usage.cache_creation_input_tokens - w1
    return (
        p.input * usage.input_tokens
        + p.cache_write_5m * w5
        + p.cache_write_1h * w1
        + p.cache_read * usage.cache_read_input_tokens
        + p.output * usage.output_tokens
    ) / 1e6


def dollars_uncached(usage: Usage, model: str | None) -> float:
    """The same tokens priced as if nothing were cached."""
    if model is None:
        return 0.0
    p = price(model)
    total_in = usage.input_tokens + usage.cache_creation_input_tokens + usage.cache_read_input_tokens
    return (p.input * total_in + p.output * usage.output_tokens) / 1e6
