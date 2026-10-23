"""The judge's prompt and its handling of verdicts. A fake `ask` stands in for the model, so nothing here costs tokens."""

import pytest
from pydantic import ValidationError

from refund_harness.agent.types import ToolSpec, Usage
from refund_harness.evals.judge import JUDGE_SCHEMA, Judge, build_judge_prompt
from refund_harness.evals.results import ToolRun

EVIDENCE = [ToolRun(name="search_policy", arguments={"question": "return window"}, output="Return window\nItems may be returned within 30 days.")]
# What the agent was told about its own tools counts as evidence about the agent ("a human must approve it").
SPECS = [ToolSpec(name="submit_order_refund", description="Submit a refund request. It stays pending until a human approves it.", parameters={})]


def test_prompt_holds_question_evidence_answer_and_tools():
    prompt = build_judge_prompt("How long do I have to return it?", EVIDENCE, "Within 30 days.", SPECS)

    assert "How long do I have to return it?" in prompt
    assert "Items may be returned within 30 days." in prompt
    assert "search_policy" in prompt
    assert "Within 30 days." in prompt
    assert "submit_order_refund" in prompt and "stays pending until a human approves it" in prompt


async def test_verdict_comes_back_with_its_usage():
    usage = Usage(input_tokens=2500, output_tokens=60, cost_usd=0.01)
    sent = []

    async def ask(prompt, schema):
        sent.append((prompt, schema))
        return {"supported": True, "declines": False, "reason": "30 days is in the evidence."}, usage

    verdict, used = await Judge(ask, "claude-opus-4-6").grade("How long?", EVIDENCE, "Within 30 days.")

    [(prompt, schema)] = sent
    assert "How long?" in prompt and schema is JUDGE_SCHEMA
    assert verdict.supported is True and verdict.declines is False
    assert verdict.reason == "30 days is in the evidence."
    assert used is usage


async def test_incomplete_verdict_is_refused():
    async def ask(prompt, schema):
        return {"supported": True}, None  # no 'declines', no 'reason'

    with pytest.raises(ValidationError):
        await Judge(ask, "m").grade("q", EVIDENCE, "a")
