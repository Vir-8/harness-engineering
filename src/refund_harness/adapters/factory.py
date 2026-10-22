"""Which model the harness talks to, chosen by environment variables, in one place (the app and the evals both use it).

    LLM_PROVIDER   openai (default) | claude | fake
    OPENAI_MODEL / OPENAI_REASONING_EFFORT     for openai (defaults: gpt-6-luna, low); needs OPENAI_API_KEY
    CLAUDE_MODEL                               for claude, through the `claude` CLI (needs a CLI login)
    FAKE_LLM_DELAY_S                           for fake: LoadTestLLM (agent/llm.py), a stand-in for load tests that
                                               calls no one
"""

import os

from refund_harness.adapters.claude_cli import DEFAULT_MODEL as DEFAULT_CLAUDE_MODEL
from refund_harness.adapters.claude_cli import ClaudeCLIClient
from refund_harness.adapters.openai_responses import (
    DEFAULT_MODEL as DEFAULT_OPENAI_MODEL,
)
from refund_harness.adapters.openai_responses import (
    DEFAULT_REASONING_EFFORT,
    OpenAIClient,
)
from refund_harness.agent.llm import LLMClient, LoadTestLLM

PROVIDERS = ("openai", "claude", "fake")
DEFAULT_PROVIDER = "openai"


def resolve_provider(provider: str | None = None) -> str:
    # `or`, not a .get default: docker compose passes LLM_PROVIDER="" when it is unset, which must mean the default too
    provider = provider or os.environ.get("LLM_PROVIDER") or DEFAULT_PROVIDER
    if provider not in PROVIDERS:
        raise ValueError(f"LLM_PROVIDER must be one of {PROVIDERS}, not {provider!r}")
    return provider


def default_model(provider: str | None = None) -> str:
    """The model create_llm uses unless told otherwise; the evals label their runs with it."""
    provider = resolve_provider(provider)
    if provider == "claude":
        return os.environ.get("CLAUDE_MODEL") or DEFAULT_CLAUDE_MODEL
    if provider == "fake":
        return LoadTestLLM.MODEL
    return os.environ.get("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL


def create_llm(provider: str | None = None, model: str | None = None) -> LLMClient:
    provider = resolve_provider(provider)
    model = model or default_model(provider)
    if provider == "claude":
        return ClaudeCLIClient(model=model)
    if provider == "fake":
        return LoadTestLLM(delay_s=float(os.environ.get("FAKE_LLM_DELAY_S") or "1.0"))
    return OpenAIClient(model=model, reasoning_effort=os.environ.get("OPENAI_REASONING_EFFORT") or DEFAULT_REASONING_EFFORT)
