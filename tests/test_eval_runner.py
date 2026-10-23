"""run_case and run_cases: the mechanics of running a case, with a scripted model and the fake embedder.

The real database is used (the tools need it); the model is a FakeLLM, so nothing here costs tokens.
"""

import pytest
from helpers import RaisingLLM

from refund_harness.agent.errors import ModelCallError
from refund_harness.agent.llm import FakeLLM
from refund_harness.agent.types import LLMResponse, ToolCall, Usage
from refund_harness.evals.cases import EvalCase
from refund_harness.evals.judge import Judge
from refund_harness.evals.results import ToolRun
from refund_harness.evals.runner import (
    count_repeated_calls,
    eval_now,
    run_case,
    run_cases,
)
from refund_harness.retrieval.embedder import FakeEmbedder

USAGE = Usage(input_tokens=3000, output_tokens=100, cost_usd=0.02)


def a_case(**overrides) -> EvalCase:
    return EvalCase(**({"id": "case-1", "merchant": "acme", "category": "order", "question": "What is the status of order A100?"} | overrides))


def asks_for_order(call_id: str = "c1") -> LLMResponse:
    return LLMResponse(tool_calls=[ToolCall(id=call_id, name="get_order", arguments={"order_id": "A100"})], usage=USAGE)


async def run(pool, case: EvalCase, llm, now, **kwargs):
    return await run_case(case, pool=pool, llm=llm, embedder=FakeEmbedder(), now=now, **kwargs)


# --- what gets recorded ---

async def test_case_records_answer_tools_and_cost(pool, refunds):
    llm = FakeLLM([asks_for_order(), LLMResponse(text="It was delivered.", usage=USAGE)])

    result = await run(pool, a_case(), llm, await eval_now(pool))

    assert result.answer == "It was delivered."
    assert result.stop_reason == "final"
    [tool] = result.tools
    assert (tool.name, tool.arguments, tool.failed) == ("get_order", {"order_id": "A100"}, False)
    assert "headphones" in tool.output
    assert "Delivered: 4 days ago" in tool.output  # the pinned clock, not the real one: the same answer every month
    assert result.model_calls == 2
    assert (result.input_tokens, result.output_tokens) == (6000, 200)
    assert result.cost_usd == pytest.approx(0.04)
    assert result.latency_s > 0


# --- the database ---

async def test_refunds_created_are_read_back(pool, refunds):
    case = a_case(question="Refund order A100.", refund_created=True, refund_amount_cents=5900)
    refund = LLMResponse(tool_calls=[ToolCall(id="c1", name="submit_order_refund", arguments={"order_id": "A100", "amount_cents": 5900})])
    llm = FakeLLM([refund, LLMResponse(text="Submitted, pending approval.")])

    result = await run(pool, case, llm, await eval_now(pool))

    assert result.refunds_cents == [5900]
    assert result.scores["refund_state"] is True


# --- retrieval, measured on the customer's own words ---

async def test_retrieval_is_measured_for_expected_chunks(pool, refunds):
    case = a_case(category="policy", question="How long do I have to return something?", expected_chunk="Return window")

    result = await run(pool, case, FakeLLM([LLMResponse(text="30 days.")]), await eval_now(pool))

    assert result.retrieval is not None
    assert len(result.retrieval.top_titles) == 3
    assert result.retrieval.rank is None or 1 <= result.retrieval.rank <= 3


# --- failures ---

async def test_model_failure_is_recorded_not_fatal(pool, refunds):
    result = await run(pool, a_case(), RaisingLLM(RuntimeError("model down")), await eval_now(pool))

    assert result.error == "RuntimeError: model down"
    assert result.scores["completed"] is False
    assert result.passed is False


def test_identical_tool_calls_are_counted_as_repeats():
    same = ToolRun(name="search_policy", arguments={"question": "return policy"}, output="x")
    other = ToolRun(name="search_policy", arguments={"question": "something else"}, output="x")

    assert count_repeated_calls([same, same, same, other]) == 2
    assert count_repeated_calls([same, other]) == 0


# --- the judge ---

async def test_judge_verdict_and_cost_are_kept_apart(pool, refunds):
    prompts = []

    async def ask(prompt, schema):
        prompts.append(prompt)
        return {"supported": True, "declines": False, "reason": "fine"}, Usage(input_tokens=2000, output_tokens=40, cost_usd=0.01)

    llm = FakeLLM([asks_for_order(), LLMResponse(text="Delivered.", usage=USAGE)])

    result = await run(pool, a_case(), llm, await eval_now(pool), judge=Judge(ask, "judge-model"))

    assert result.judge.supported is True
    assert "submit_order_refund" in prompts[0]  # the judge was told what the agent's tools do
    assert result.judge_cost_usd == pytest.approx(0.01)
    assert result.cost_usd == pytest.approx(0.04)  # the agent's cost only
    assert result.scores["evidence_supported"] is True


async def test_broken_judge_does_not_fail_the_agent(pool, refunds):
    async def ask(prompt, schema):
        raise RuntimeError("judge down")

    llm = FakeLLM([asks_for_order(), LLMResponse(text="Delivered.")])

    result = await run(pool, a_case(), llm, await eval_now(pool), judge=Judge(ask, "judge-model"))

    assert result.judge is None and result.judge_error == "RuntimeError: judge down"
    assert result.scores["evidence_supported"] is None
    assert result.passed is True


# --- several cases and repeats ---

async def test_each_case_runs_once_per_repeat(pool, refunds):
    cases = [a_case(id="case-1"), a_case(id="case-2")]
    llm = FakeLLM([LLMResponse(text="a")] * 4)
    seen = []

    runs = await run_cases(cases, repeats=2, pool=pool, llm=llm, embedder=FakeEmbedder(), now=await eval_now(pool), on_result=seen.append)

    assert [(r.case_id, r.repeat) for r in runs] == [("case-1", 1), ("case-2", 1), ("case-1", 2), ("case-2", 2)]
    assert seen == runs


# --- infrastructure errors are retried; the agent's own failures are not ---

class FlakyLLM:
    """Fails its first `failures` calls with a model-call error, then behaves like the wrapped model."""

    def __init__(self, inner, failures: int, error: Exception = ModelCallError("claude exited 1: ")):
        self.inner, self.failures_left, self.error, self.calls = inner, failures, error, 0

    async def complete(self, messages, tools):
        self.calls += 1
        if self.failures_left:
            self.failures_left -= 1
            raise self.error
        return await self.inner.complete(messages, tools)


async def test_flaky_model_call_is_retried(pool, refunds):
    llm = FlakyLLM(FakeLLM([LLMResponse(text="It was delivered.", usage=USAGE)]), failures=1)

    result = await run(pool, a_case(), llm, await eval_now(pool))

    assert result.error is None and result.answer == "It was delivered."
    assert result.attempts == 2
    assert result.scores["completed"] is True


@pytest.mark.parametrize("retries", [0, 2], ids=["no_retries", "two_retries"])
async def test_retries_run_out_then_failure_is_reported(pool, refunds, retries):
    llm = FlakyLLM(FakeLLM([]), failures=99)

    result = await run(pool, a_case(), llm, await eval_now(pool), retries=retries)

    assert result.attempts == retries + 1 and llm.calls == retries + 1
    assert result.error == "ModelCallError: claude exited 1: "
    assert result.scores["completed"] is False


async def test_a_bug_is_not_retried(pool, refunds):
    llm = FlakyLLM(FakeLLM([]), failures=99, error=KeyError("a bug, not infrastructure"))

    result = await run(pool, a_case(), llm, await eval_now(pool), retries=3)

    assert result.attempts == 1 and llm.calls == 1


async def test_each_attempt_starts_with_empty_tables(pool, refunds):
    refund = LLMResponse(tool_calls=[ToolCall(id="c1", name="submit_order_refund", arguments={"order_id": "A100", "amount_cents": 5900})])

    class FailsAfterRefunding:
        calls = 0

        async def complete(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return refund  # the first attempt submits a refund...
            if self.calls == 2:
                raise ModelCallError("claude exited 1: ")  # ...then the next model call dies
            return LLMResponse(text="Nothing to do.")  # the second attempt submits nothing

    result = await run(pool, a_case(question="Refund A100."), FailsAfterRefunding(), await eval_now(pool))

    assert result.attempts == 2
    assert result.refunds_cents == []  # the first attempt's refund was cleared, so it cannot leak into this result
