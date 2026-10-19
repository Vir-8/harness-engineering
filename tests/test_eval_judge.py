"""The judge's prompt and its handling of verdicts. A fake `ask` stands in for the model, so nothing here costs tokens."""

import pytest
from pydantic import ValidationError

from refund_harness.agent.types import ToolSpec, Usage
from refund_harness.evals.judge import JUDGE_SCHEMA, Judge, build_judge_prompt
from refund_harness.evals.results import ToolRun

EVIDENCE = [ToolRun(name="search_policy", arguments={"question": "return window"}, output="Return window\nItems may be returned within 30 days.")]


def test_the_prompt_contains_the_question_the_evidence_and_the_answer():
    prompt = build_judge_prompt("How long do I have to return it?", EVIDENCE, "Within 30 days.")

    assert "How long do I have to return it?" in prompt
    assert "Items may be returned within 30 days." in prompt
    assert "search_policy" in prompt
    assert "Within 30 days." in prompt


def test_with_no_tools_the_judge_is_told_there_was_no_evidence():
    prompt = build_judge_prompt("What is the weather?", [], "It is sunny.")

    assert "no evidence at all" in prompt


def test_the_judge_is_told_to_use_only_the_evidence():
    assert "ONLY against the evidence" in build_judge_prompt("q", [], "a")


def test_the_schema_asks_for_both_verdicts_and_a_reason():
    assert set(JUDGE_SCHEMA["required"]) == {"supported", "declines", "reason"}


async def test_a_verdict_is_read_from_the_models_object_with_its_usage():
    usage = Usage(input_tokens=2500, output_tokens=60, cost_usd=0.01)

    async def ask(prompt, schema):
        return {"supported": True, "declines": False, "reason": "30 days is in the evidence."}, usage

    verdict, used = await Judge(ask, "claude-opus-4-6").grade("q", EVIDENCE, "Within 30 days.")

    assert verdict.supported is True and verdict.declines is False
    assert verdict.reason == "30 days is in the evidence."
    assert used is usage


async def test_what_the_judge_is_shown_is_what_gets_sent():
    sent = []

    async def ask(prompt, schema):
        sent.append((prompt, schema))
        return {"supported": True, "declines": False, "reason": "ok"}, None

    await Judge(ask, "m").grade("How long?", EVIDENCE, "30 days.")

    [(prompt, schema)] = sent
    assert "How long?" in prompt and schema is JUDGE_SCHEMA


async def test_an_incomplete_verdict_is_refused_rather_than_guessed():
    async def ask(prompt, schema):
        return {"supported": True}, None  # no 'declines', no 'reason'

    with pytest.raises(ValidationError):
        await Judge(ask, "m").grade("q", EVIDENCE, "a")


# --- what the agent was told about its own tools counts as evidence about the agent ---

SPECS = [ToolSpec(name="submit_order_refund", description="Submit a refund request. It stays pending until a human approves it.", parameters={})]


def test_the_judge_is_shown_what_the_tools_do_so_claims_about_the_agent_itself_can_be_supported():
    prompt = build_judge_prompt("Can you refund me?", [], "I will submit a request; a human must approve it.", SPECS)

    assert "submit_order_refund" in prompt and "stays pending until a human approves it" in prompt


def test_without_tool_descriptions_the_prompt_still_builds():
    assert "(none)" in build_judge_prompt("q", [], "a")


async def test_grading_passes_the_tool_descriptions_to_the_prompt():
    sent = []

    async def ask(prompt, schema):
        sent.append(prompt)
        return {"supported": True, "declines": False, "reason": "ok"}, None

    await Judge(ask, "m").grade("q", EVIDENCE, "a", SPECS)

    assert "stays pending until a human approves it" in sent[0]
