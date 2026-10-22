"""create_llm: LLM_PROVIDER and the per-provider model variables pick the model client."""

import pytest

from refund_harness.adapters.claude_cli import ClaudeCLIClient
from refund_harness.adapters.factory import create_llm, default_model
from refund_harness.adapters.openai_responses import OpenAIClient
from refund_harness.agent.llm import LoadTestLLM


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("LLM_PROVIDER", "OPENAI_MODEL", "OPENAI_REASONING_EFFORT", "CLAUDE_MODEL"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    "provider",
    [None, ""],  # "" is what docker compose passes when LLM_PROVIDER is unset
    ids=["unset", "empty"],
)
def test_default_is_gpt_6_luna_at_low_effort(monkeypatch, provider):
    if provider is not None:
        monkeypatch.setenv("LLM_PROVIDER", provider)

    llm = create_llm()

    assert isinstance(llm, OpenAIClient)
    assert (llm.model, llm.reasoning_effort) == ("gpt-6-luna", "low")


def test_openai_settings_come_from_the_env(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "gpt-6.1-sol")
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "medium")

    llm = create_llm()

    assert (llm.model, llm.reasoning_effort) == ("gpt-6.1-sol", "medium")
    assert default_model() == "gpt-6.1-sol"


def test_claude_through_the_cli(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "claude")
    monkeypatch.setenv("CLAUDE_MODEL", "claude-haiku-5-5")

    llm = create_llm()

    assert isinstance(llm, ClaudeCLIClient)
    assert llm.model == "claude-haiku-5-5"


def test_explicit_choice_beats_the_env(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "fake")

    assert create_llm("claude", "claude-opus-4-6").model == "claude-opus-4-6"
    assert isinstance(create_llm(), LoadTestLLM)


def test_unknown_provider_is_refused():
    with pytest.raises(ValueError, match="LLM_PROVIDER"):
        create_llm("gemini")
