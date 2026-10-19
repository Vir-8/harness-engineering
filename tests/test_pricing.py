"""estimate_cost: dollars from token counts and the published prices. The numbers below are a real call's (a one-word reply)."""

import pytest

from refund_harness.agent.pricing import PRICES, estimate_cost
from refund_harness.agent.types import Usage

# What the CLI reported for the same one-word reply on two different models: identical tokens.
ONE_WORD_REPLY = dict(input_tokens=6 + 6930 + 6862, output_tokens=56, cache_read_tokens=6862, cache_write_tokens=6930, cache_write_1h_tokens=6930)


def usage_for(model: str | None, **overrides) -> Usage:
    return Usage(**({"model": model} | ONE_WORD_REPLY | overrides))


def test_the_published_prices_give_the_hand_checked_cost_for_sonnet():
    # 6*$2 + 6930*$4 (1-hour cache write) + 6862*$0.10 (cache read) + 56*$10, per million tokens
    assert estimate_cost(usage_for("claude-sonnet-5-5")) == pytest.approx(0.028978, abs=1e-6)


def test_the_published_prices_give_the_hand_checked_cost_for_haiku():
    assert estimate_cost(usage_for("claude-haiku-5-5")) == pytest.approx(0.001483, abs=1e-6)


def test_identical_tokens_cost_different_amounts_on_different_models():
    """The Claude CLI said $0.04817 for Haiku and $0.04818 for Sonnet on these very tokens: it ignores the model."""
    sonnet, haiku = estimate_cost(usage_for("claude-sonnet-5-5")), estimate_cost(usage_for("claude-haiku-5-5"))

    assert sonnet > 10 * haiku


def test_cache_writes_with_the_short_lifetime_are_priced_lower_than_the_long_one():
    long_lived = estimate_cost(usage_for("claude-sonnet-5-5", cache_write_1h_tokens=6930))
    short_lived = estimate_cost(usage_for("claude-sonnet-5-5", cache_write_1h_tokens=0))

    assert short_lived < long_lived


def test_a_model_that_is_not_in_the_table_has_an_unknown_cost_not_a_guess():
    assert estimate_cost(usage_for("claude-mystery-9")) is None
    assert estimate_cost(usage_for(None)) is None


def test_haiku_prompts_over_its_tier_limit_are_not_guessed():
    assert estimate_cost(usage_for("claude-haiku-5-5", input_tokens=150_000)) is None


def test_a_call_with_no_cache_is_just_input_and_output():
    usage = Usage(model="claude-sonnet-5-5", input_tokens=1000, output_tokens=100)

    assert estimate_cost(usage) == pytest.approx((1000 * 2.0 + 100 * 10.0) / 1_000_000)


def test_every_listed_model_has_sane_prices():
    for model, price in PRICES.items():
        assert price.cache_read < price.input < price.cache_write_5m < price.cache_write_1h < price.output, model
