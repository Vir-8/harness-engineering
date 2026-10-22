"""Prometheus metrics: what gets counted, and that labels stay few (route templates, not raw paths).

Metrics are process-wide and only go up, so each test reads a value before and after and checks the difference.
"""

import json

import pytest
from helpers import ACME, RaisingLLM, approved_refund
from prometheus_client import REGISTRY

from refund_harness.agent.llm import FakeLLM
from refund_harness.agent.loop import run_agent
from refund_harness.agent.types import LLMResponse, ToolCall, Usage
from refund_harness.cache.embedding_cache import CachedEmbedder
from refund_harness.jobs.worker import process_batch, update_queue_gauges
from refund_harness.retrieval.embedder import FakeEmbedder


def value(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


class Change:
    """The difference in one metric between creating this and calling it."""

    def __init__(self, name: str, **labels):
        self.name, self.labels = name, labels
        self.before = value(name, **labels)

    def __call__(self) -> float:
        return value(self.name, **self.labels) - self.before


USAGE = Usage(model="m-test", input_tokens=100, output_tokens=10, cost_usd=0.5)  # what each scripted model call reports


def tool_call(name: str, arguments: dict) -> LLMResponse:
    return LLMResponse(tool_calls=[ToolCall(id="c1", name=name, arguments=arguments)], usage=USAGE)


def answer(text: str) -> LLMResponse:
    return LLMResponse(text=text, usage=USAGE)


# --- HTTP ---

async def test_requests_are_counted_by_route_template(client):
    approve_404s = Change("http_requests_total", method="POST", route="/refunds/{refund_id}/approve", status="404")
    unmatched = Change("http_requests_total", method="GET", route="unmatched", status="404")

    for refund_id in (991, 992):
        await client.post(f"/refunds/{refund_id}/approve", headers={"X-Reviewer-Id": "r"})
    await client.get("/no-such-page-1")
    await client.get("/no-such-page-2")

    assert approve_404s() == 2  # both ids in ONE series
    assert unmatched() == 2  # made-up URLs share one series too, or anyone could create endless series
    page = (await client.get("/metrics")).text
    assert "/refunds/991/approve" not in page and "no-such-page" not in page


# --- the agent ---

async def test_agent_run_counts_calls_tokens_and_cost(registry):
    runs = Change("agent_runs_total", stop_reason="final")
    calls = Change("llm_calls_total", model="m-test", outcome="ok")
    input_tokens = Change("llm_tokens_total", model="m-test", direction="input")
    cost = Change("llm_cost_usd_total", model="m-test")
    tools = Change("agent_tool_calls_total", tool="get_order", outcome="ok")
    llm = FakeLLM([tool_call("get_order", {"order_id": "A100"}), answer("done")])

    await run_agent(llm, registry, "where is A100?")

    assert (runs(), calls(), input_tokens(), cost(), tools()) == (1, 2, 200, 1.0, 1)


async def test_failed_tool_calls_are_counted_by_outcome(registry):
    crashed = Change("agent_tool_calls_total", tool="get_order", outcome="crashed")
    invalid = Change("agent_tool_calls_total", tool="get_order", outcome="invalid_arguments")  # the model's mistake, not a fault
    invented = Change("agent_tool_calls_total", tool="(unknown)", outcome="unknown_tool")
    llm = FakeLLM([
        tool_call("get_order", {"order_id": "Z999"}),
        tool_call("get_order", {"id": "A100"}),
        tool_call("made_up_tool", {}),
        LLMResponse(text="sorry"),
    ])

    await run_agent(llm, registry, "?")

    assert (crashed(), invalid(), invented()) == (1, 1, 1)
    assert value("agent_tool_calls_total", tool="made_up_tool", outcome="unknown_tool") == 0


async def test_failed_model_call_is_an_error_run(registry):
    errors = Change("llm_calls_total", model="unknown", outcome="error")
    error_runs = Change("agent_runs_total", stop_reason="error")

    with pytest.raises(RuntimeError):
        await run_agent(RaisingLLM(RuntimeError("down")), registry, "?")

    assert (errors(), error_runs()) == (1, 1)


# --- cache and worker ---

async def test_cache_hits_and_misses_are_counted(redis_client):
    hits, misses = Change("embedding_cache_lookups_total", outcome="hit"), Change("embedding_cache_lookups_total", outcome="miss")
    cached = CachedEmbedder(FakeEmbedder(), redis_client)

    for _ in range(3):
        await cached.embed_query("same question")

    assert (misses(), hits()) == (1, 2)


async def test_payout_jobs_are_counted_with_wait_time(pool, refunds, provider, sqs, payout_queues):
    issued = Change("payout_jobs_total", outcome="issued")
    nothing = Change("payout_jobs_total", outcome="nothing_to_do")
    waits = Change("payout_job_wait_seconds_count")
    refund_id = await approved_refund(pool)
    for _ in range(2):  # the second is a duplicate: already issued by then
        sqs.send_message(QueueUrl=payout_queues.url, MessageBody=json.dumps({"merchant_id": ACME, "refund_id": refund_id}))

    while await process_batch(pool, provider, sqs, payout_queues, wait_s=0):
        pass

    assert (issued(), nothing(), waits()) == (1, 1, 2)


def test_queue_depth_is_measured(sqs, payout_queues):
    sqs.send_message(QueueUrl=payout_queues.url, MessageBody="{}")

    update_queue_gauges(sqs, payout_queues)

    assert value("payout_queue_messages", queue="payouts", state="waiting") == 1
    assert value("payout_queue_messages", queue="dead_letters", state="waiting") == 0
