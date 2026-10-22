"""What a model call cost, computed from its token counts and the provider's PUBLISHED prices.

Why not use the cost the Claude CLI reports? Because it is not model-aware: for the identical token counts it reported
$0.04817 on Haiku 5.5 and $0.04818 on Sonnet 5.5, when the published prices make those tokens worth about $0.0015 and
$0.029. Langfuse's price table matches the published one. Token counts are facts; dollars depend on a price table, so
the table is written down here, with its source, and an unknown model has an UNKNOWN cost (None), never a guess.
"""

import re
from dataclasses import dataclass

from refund_harness.agent.types import Usage


@dataclass(frozen=True)
class ModelPrice:
    """Dollars per MILLION tokens."""

    input: float
    cache_write_5m: float  # the provider's short-lived cache writes (Anthropic: 5 minutes; OpenAI: 30 minutes)
    cache_write_1h: float  # its long-lived ones (Anthropic: 1 hour; OpenAI: 12 hours)
    cache_read: float
    output: float
    max_prompt_tokens: int | None = None  # above this the model has a different (higher) price tier, so we do not guess


# Source: https://platform.claude.com/docs/en/about-claude/pricing (checked 2026-10-19). Update it when prices change.
PRICES = {
    "claude-sonnet-5-5": ModelPrice(input=2.0, cache_write_5m=2.5, cache_write_1h=4.0, cache_read=0.10, output=10.0),
    "claude-haiku-5-5": ModelPrice(input=0.10, cache_write_5m=0.125, cache_write_1h=0.20, cache_read=0.01, output=0.50, max_prompt_tokens=100_000),
    "claude-opus-4-6": ModelPrice(input=5.0, cache_write_5m=6.25, cache_write_1h=10.0, cache_read=0.50, output=25.0),
    # Source: https://developers.openai.com/api/docs/pricing (checked 2026-10-23), standard tier, short context.
    # Reasoning tokens are part of output_tokens and billed as output.
    "gpt-6-luna": ModelPrice(input=0.10, cache_write_5m=0.125, cache_write_1h=0.25, cache_read=0.01, output=0.50, max_prompt_tokens=272_000),
    "gpt-6.1-sol": ModelPrice(input=2.0, cache_write_5m=2.5, cache_write_1h=5.0, cache_read=0.10, output=10.0, max_prompt_tokens=272_000),
}

_SNAPSHOT_DATE = re.compile(r"-\d{4}-\d{2}-\d{2}$")  # OpenAI reports the exact snapshot, e.g. gpt-6-luna-2026-09-22


def price_for(model: str | None) -> ModelPrice | None:
    if not model:
        return None
    return PRICES.get(model) or PRICES.get(_SNAPSHOT_DATE.sub("", model))


def estimate_cost(usage: Usage) -> float | None:
    """Dollars for this call, or None when the model is not in the table or the call is outside its known price tier."""
    price = price_for(usage.model)
    if price is None or (price.max_prompt_tokens is not None and usage.input_tokens > price.max_prompt_tokens):
        return None
    cache_writes_5m = usage.cache_write_tokens - usage.cache_write_1h_tokens
    uncached_input = usage.input_tokens - usage.cache_read_tokens - usage.cache_write_tokens
    millionths = (
        uncached_input * price.input
        + cache_writes_5m * price.cache_write_5m
        + usage.cache_write_1h_tokens * price.cache_write_1h
        + usage.cache_read_tokens * price.cache_read
        + usage.output_tokens * price.output
    )
    return round(millionths / 1_000_000, 6)
