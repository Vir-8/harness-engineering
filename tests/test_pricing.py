"""estimate_cost: dollars from token counts and the published prices. The numbers below are a real call's (a one-word reply)."""

import pytest

from refund_harness.agent.pricing import PRICES, estimate_cost
from refund_harness.agent.types import Usage

# What the CLI reported for the same one-word reply on two different models: identical tokens.
ONE_WORD_REPLY = {
    "input_tokens": 6 + 6930 + 6862,
    "output_tokens": 56,
    "cache_read_tokens": 6862,
    "cache_write_tokens": 6930,
    "cache_write_1h_tokens": 6930,
}


def usage_for(model: str | None, **overrides) -> Usage:
    return Usage(**({"model": model} | ONE_WORD_REPLY | overrides))


@pytest.mark.parametrize(
    "model, dollars",
    [
        # 6*$2 + 6930*$4 (1-hour cache write) + 6862*$0.10 (cache read) + 56*$10, per million tokens
        ("claude-sonnet-5-5", 0.028978),
        ("claude-haiku-5-5", 0.001483),
    ],
    ids=["sonnet", "haiku"],
)
def test_cost_matches_the_hand_checked_figure(model, dollars):
    """The Claude CLI said $0.048 for both on these very tokens: its own figure ignores the model."""
    assert estimate_cost(usage_for(model)) == pytest.approx(dollars, abs=1e-6)


def test_short_cache_writes_cost_less():
    long_lived = estimate_cost(usage_for("claude-sonnet-5-5", cache_write_1h_tokens=6930))
    short_lived = estimate_cost(usage_for("claude-sonnet-5-5", cache_write_1h_tokens=0))

    assert short_lived < long_lived


@pytest.mark.parametrize(
    "usage",
    [
        usage_for("claude-mystery-9"),
        usage_for(None),
        usage_for("claude-haiku-5-5", input_tokens=150_000),  # over Haiku's price tier limit
    ],
    ids=["model_not_in_the_table", "no_model_named", "haiku_over_its_tier_limit"],
)
def test_unknown_cost_is_none_not_a_guess(usage):
    assert estimate_cost(usage) is None


def test_every_listed_model_has_sane_prices():
    for model, price in PRICES.items():
        assert price.cache_read < price.input < price.cache_write_5m < price.cache_write_1h < price.output, model
