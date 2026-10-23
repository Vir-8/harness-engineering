"""The agent loop: model, tools, model, ... until an answer; failures it absorbs; and what it leaves in a trace.

The trace is invoke_agent > chat / execute_tool <name>. Most trace tests use a fake model that asks for two tools in
one turn, get_order (works) and search_policy (crashes), and then answers.
"""

import asyncio
import json

import pytest
from helpers import RaisingLLM
from opentelemetry.trace import StatusCode
from pydantic import BaseModel

from refund_harness.agent.errors import ModelCallError
from refund_harness.agent.llm import FakeLLM
from refund_harness.agent.loop import MAX_STEPS, run_agent
from refund_harness.agent.tools import Tool, ToolRegistry
from refund_harness.agent.types import LLMResponse, ToolCall, Usage

ORDERS = {"A100": "headphones, delivered"}
CRASH_MESSAGE = "the policy table is gone"


class OrderArgs(BaseModel):
    order_id: str


class PolicyArgs(BaseModel):
    question: str


async def get_order(args: OrderArgs) -> str:
    return ORDERS[args.order_id]  # KeyError for unknown orders, on purpose


async def search_policy(args: PolicyArgs) -> str:
    raise KeyError(CRASH_MESSAGE)


TOOLS = ToolRegistry([
    Tool("get_order", "Look up an order.", OrderArgs, get_order),
    Tool("search_policy", "Search the policy.", PolicyArgs, search_policy),
])
LOOKUP = ToolCall(id="c1", name="get_order", arguments={"order_id": "A100"})


def call(name: str, args: dict, id: str = "c1") -> LLMResponse:
    return LLMResponse(tool_calls=[ToolCall(id=id, name=name, arguments=args)])


def model_that_calls_both_tools() -> FakeLLM:
    return FakeLLM([
        LLMResponse(tool_calls=[LOOKUP, ToolCall(id="c2", name="search_policy", arguments={"question": "returns?"})]),
        LLMResponse(text="All done."),
    ])


def named(spans, name: str):
    [span] = [s for s in spans() if s.name == name]
    return span


# --- the loop ---

async def test_tool_result_goes_back_to_the_model():
    llm = FakeLLM([call("get_order", {"order_id": "A100"}), LLMResponse(text="Order A100 was delivered")])
    result = await run_agent(llm, TOOLS, "Where is order A100?")

    assert result.stop_reason == "final"
    assert result.final_text == "Order A100 was delivered"
    # the 2nd model call must have SEEN the tool output
    assert llm.calls[1][-1].role == "tool"
    assert llm.calls[1][-1].content == "headphones, delivered"


@pytest.mark.parametrize(
    "bad_call, model_sees",
    [
        (ToolCall(id="c1", name="nope", arguments={}), "unknown tool"),
        (ToolCall(id="c1", name="get_order", arguments={"id": "A100"}), "order_id"),  # tells the model WHICH field was wrong
        (ToolCall(id="c1", name="search_policy", arguments={"question": "returns?"}), "failed"),
    ],
    ids=["unknown_tool", "invalid_arguments", "tool_crashed"],
)
async def test_failed_tool_call_is_told_to_the_model(bad_call, model_sees):
    # The model made a mistake, or a tool broke: the model is told so it can react, and the run is not blown up.
    llm = FakeLLM([LLMResponse(tool_calls=[bad_call]), LLMResponse(text="sorry")])
    result = await run_agent(llm, TOOLS, "Where is order A100?")

    tool_msg = llm.calls[1][-1]
    assert tool_msg.role == "tool" and tool_msg.tool_call_id == "c1"
    assert model_sees in tool_msg.content.lower()
    assert CRASH_MESSAGE not in tool_msg.content  # an exception's own text can hold internals the model might repeat
    assert result.stop_reason == "final"


class AlwaysCallsTool:
    async def complete(self, messages, tools):
        return call("get_order", {"order_id": "A100"})


async def test_max_steps_stops_a_runaway_loop():
    # wait_for makes this test FAIL after 1s instead of hanging forever
    result = await asyncio.wait_for(run_agent(AlwaysCallsTool(), TOOLS, "Where is order A100?"), timeout=1)

    assert result.stop_reason == "max_steps"
    assert sum(m.role == "assistant" for m in result.messages) == MAX_STEPS


async def test_run_keeps_a_timeline_of_every_step():
    llm = FakeLLM([
        call("get_order", {"order_id": "A100"}),
        call("get_order", {"order_id": "Z999"}, id="c2"),
        call("nope", {}, id="c3"),
        LLMResponse(text="done"),
    ])

    result = await run_agent(llm, TOOLS, "?")

    assert [(s.kind, s.name, s.failed) for s in result.steps] == [
        ("model", "model", False), ("tool", "get_order", False),
        ("model", "model", False), ("tool", "get_order", True),  # Z999 makes the tool crash
        ("model", "model", False), ("tool", "nope", True),  # a tool that does not exist
        ("model", "model", False),
    ]
    assert result.steps[1].output == "headphones, delivered"
    assert result.steps[-1].output == "done"
    assert all(s.duration_ms >= 0 for s in result.steps)


# --- what the loop leaves in a trace ---

async def test_trace_nests_model_and_tool_calls(spans):
    await run_agent(model_that_calls_both_tools(), TOOLS, "Where is A100?")

    assert [s.name for s in spans()] == [
        "invoke_agent", "chat", "execute_tool get_order", "execute_tool search_policy", "chat",
    ]
    agent = spans()[0]
    assert all(s.parent.span_id == agent.context.span_id for s in spans()[1:])
    # each span says what kind of work it is, in the GenAI conventions' words
    assert agent.attributes["gen_ai.operation.name"] == "invoke_agent"
    assert agent.attributes["gen_ai.agent.name"] == "refund-agent"
    assert named(spans, "execute_tool get_order").attributes["gen_ai.operation.name"] == "execute_tool"
    assert named(spans, "execute_tool get_order").attributes["gen_ai.tool.name"] == "get_order"
    assert {s.attributes["gen_ai.operation.name"] for s in spans() if s.name == "chat"} == {"chat"}


async def test_crashing_tool_is_an_error_span(spans):
    await run_agent(model_that_calls_both_tools(), TOOLS, "Where is A100?")

    crashed = named(spans, "execute_tool search_policy")
    assert crashed.status.status_code == StatusCode.ERROR
    assert [e.name for e in crashed.events] == ["exception"]
    assert named(spans, "execute_tool get_order").status.status_code != StatusCode.ERROR
    assert named(spans, "invoke_agent").status.status_code != StatusCode.ERROR  # the loop handled the crash


async def test_failing_model_call_is_an_error_span(spans):
    with pytest.raises(ModelCallError):
        await run_agent(RaisingLLM(ModelCallError("down")), TOOLS, "Where is A100?")

    assert named(spans, "chat").status.status_code == StatusCode.ERROR
    assert named(spans, "invoke_agent").status.status_code == StatusCode.ERROR


USED = Usage(input_tokens=100, output_tokens=10, cost_usd=0.01)


@pytest.mark.parametrize(
    "replies, stop_reason, tool_rounds",
    [
        ([LLMResponse(tool_calls=[LOOKUP], usage=USED), LLMResponse(text="Done.", usage=USED)], "final", 1),
        ([LLMResponse(tool_calls=[LOOKUP], usage=USED)] * 3, "max_steps", 2),
    ],
    ids=["answered", "ran_out_of_steps"],
)
async def test_agent_span_records_outcome_and_totals(spans, replies, stop_reason, tool_rounds):
    await run_agent(FakeLLM(replies), TOOLS, "Where is A100?", max_steps=2)

    agent = named(spans, "invoke_agent")
    assert agent.attributes["app.stop_reason"] == stop_reason
    assert agent.attributes["app.steps"] == tool_rounds
    assert agent.attributes["app.input_tokens"] == 200  # two model calls either way: totals don't need a final answer


async def test_content_is_recorded_on_the_spans(spans):
    await run_agent(model_that_calls_both_tools(), TOOLS, "Where is A100?")

    first_chat = next(s for s in spans() if s.name == "chat")
    assert json.loads(first_chat.attributes["gen_ai.input.messages"])[0]["parts"][0]["content"] == "Where is A100?"
    worked = named(spans, "execute_tool get_order")
    assert json.loads(worked.attributes["gen_ai.tool.call.arguments"]) == {"order_id": "A100"}
    assert json.loads(worked.attributes["gen_ai.tool.call.result"]) == "headphones, delivered"
    crashed = named(spans, "execute_tool search_policy")
    assert "gen_ai.tool.call.arguments" in crashed.attributes
    assert "gen_ai.tool.call.result" not in crashed.attributes  # nothing came back


async def test_content_tracing_off_records_no_content(spans, monkeypatch):
    monkeypatch.setenv("TRACE_CONTENT", "off")

    await run_agent(model_that_calls_both_tools(), TOOLS, "Where is A100?")

    everything = str([dict(s.attributes) for s in spans()])
    assert "Where is A100?" not in everything
    assert "headphones" not in everything


async def test_usage_is_recorded_per_call_and_in_total(spans):
    llm = FakeLLM([
        LLMResponse(
            tool_calls=[LOOKUP],
            usage=Usage(model="claude-opus-4-6", input_tokens=1000, output_tokens=50, cost_usd=0.02),
        ),
        LLMResponse(text="Done.", usage=Usage(model="claude-opus-4-6", input_tokens=1200, output_tokens=30, cost_usd=0.03)),
    ])

    await run_agent(llm, TOOLS, "Where is A100?")

    first_chat, second_chat = [s for s in spans() if s.name == "chat"]
    assert first_chat.attributes["gen_ai.usage.input_tokens"] == 1000
    assert second_chat.attributes["gen_ai.usage.input_tokens"] == 1200
    agent = named(spans, "invoke_agent")
    assert agent.attributes["app.input_tokens"] == 2200
    assert agent.attributes["app.output_tokens"] == 80
    assert agent.attributes["app.cost_usd"] == 0.05
    assert "gen_ai.usage.input_tokens" not in agent.attributes  # only the chat spans carry it, so nothing is counted twice


async def test_crashing_tool_is_reported_to_sentry_once(spans, sentry_events):
    await run_agent(model_that_calls_both_tools(), TOOLS, "Where is A100?")

    [event] = sentry_events.events  # get_order worked, so it sent nothing
    assert event["tags"]["tool.name"] == "search_policy"
    assert event["tags"]["error.type"] == "KeyError"
    assert event["contexts"]["tool_call"]["arguments"] == {"question": "returns?"}
