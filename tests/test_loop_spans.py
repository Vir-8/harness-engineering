"""What run_agent leaves in a trace: invoke_agent > chat / execute_tool <name>, with errors and content recorded.

A fake model asks for two tools in one turn: get_order works, search_policy crashes. Then it gives its answer.
"""

import json

import pytest
from opentelemetry.trace import StatusCode
from pydantic import BaseModel

from refund_harness.errors import ModelCallError
from refund_harness.llm import FakeLLM, RaisingLLM
from refund_harness.loop import run_agent
from refund_harness.tools import Tool, ToolRegistry
from refund_harness.types import LLMResponse, ToolCall, Usage


class Args(BaseModel):
    order_id: str


async def get_order(args: Args) -> str:
    return "headphones, delivered"


async def search_policy(args: Args) -> str:
    raise KeyError("the policy table is gone")


TOOLS = ToolRegistry([
    Tool("get_order", "Look up an order.", Args, get_order),
    Tool("search_policy", "Search the policy.", Args, search_policy),
])


def model_that_calls_both_tools() -> FakeLLM:
    return FakeLLM([
        LLMResponse(tool_calls=[
            ToolCall(id="c1", name="get_order", arguments={"order_id": "A100"}),
            ToolCall(id="c2", name="search_policy", arguments={"order_id": "A100"}),
        ]),
        LLMResponse(text="All done."),
    ])


def named(spans, name: str):
    [span] = [s for s in spans() if s.name == name]
    return span


async def test_the_trace_is_one_agent_span_with_each_model_call_and_tool_call_inside(spans):
    await run_agent(model_that_calls_both_tools(), TOOLS, "Where is A100?")

    assert [s.name for s in spans()] == [
        "invoke_agent", "chat", "execute_tool get_order", "execute_tool search_policy", "chat",
    ]
    agent = spans()[0]
    assert all(s.parent.span_id == agent.context.span_id for s in spans()[1:])


async def test_spans_say_what_kind_of_work_they_are(spans):
    await run_agent(model_that_calls_both_tools(), TOOLS, "Where is A100?")

    assert named(spans, "invoke_agent").attributes["gen_ai.operation.name"] == "invoke_agent"
    assert named(spans, "invoke_agent").attributes["gen_ai.agent.name"] == "refund-agent"
    assert named(spans, "execute_tool get_order").attributes["gen_ai.operation.name"] == "execute_tool"
    assert named(spans, "execute_tool get_order").attributes["gen_ai.tool.name"] == "get_order"
    assert {s.attributes["gen_ai.operation.name"] for s in spans() if s.name == "chat"} == {"chat"}


async def test_a_crashing_tool_shows_up_as_an_error_in_the_trace(spans):
    await run_agent(model_that_calls_both_tools(), TOOLS, "Where is A100?")

    crashed = named(spans, "execute_tool search_policy")
    assert crashed.status.status_code == StatusCode.ERROR
    assert [e.name for e in crashed.events] == ["exception"]
    assert named(spans, "execute_tool get_order").status.status_code != StatusCode.ERROR
    assert named(spans, "invoke_agent").status.status_code != StatusCode.ERROR  # the loop handled the crash


async def test_a_failing_model_call_is_an_error_on_the_chat_span_and_the_agent_span(spans):
    with pytest.raises(ModelCallError):
        await run_agent(RaisingLLM(ModelCallError("down")), TOOLS, "Where is A100?")

    assert named(spans, "chat").status.status_code == StatusCode.ERROR
    assert named(spans, "invoke_agent").status.status_code == StatusCode.ERROR


async def test_the_agent_span_records_how_the_run_ended(spans):
    await run_agent(model_that_calls_both_tools(), TOOLS, "Where is A100?")

    assert named(spans, "invoke_agent").attributes["app.stop_reason"] == "final"
    assert named(spans, "invoke_agent").attributes["app.steps"] == 1


async def test_running_out_of_steps_is_visible_on_the_agent_span(spans):
    endless = FakeLLM([
        LLMResponse(tool_calls=[ToolCall(id=f"c{i}", name="get_order", arguments={"order_id": "A100"})])
        for i in range(3)
    ])

    await run_agent(endless, TOOLS, "Where is A100?", max_steps=2)

    assert named(spans, "invoke_agent").attributes["app.stop_reason"] == "max_steps"


async def test_content_is_recorded_on_the_spans(spans):
    await run_agent(model_that_calls_both_tools(), TOOLS, "Where is A100?")

    first_chat = [s for s in spans() if s.name == "chat"][0]
    assert json.loads(first_chat.attributes["gen_ai.input.messages"])[0]["parts"][0]["content"] == "Where is A100?"
    worked = named(spans, "execute_tool get_order")
    assert json.loads(worked.attributes["gen_ai.tool.call.arguments"]) == {"order_id": "A100"}
    assert json.loads(worked.attributes["gen_ai.tool.call.result"]) == "headphones, delivered"
    crashed = named(spans, "execute_tool search_policy")
    assert "gen_ai.tool.call.arguments" in crashed.attributes
    assert "gen_ai.tool.call.result" not in crashed.attributes  # nothing came back


async def test_with_content_tracing_off_no_content_is_recorded_anywhere(spans, monkeypatch):
    monkeypatch.setenv("TRACE_CONTENT", "off")

    await run_agent(model_that_calls_both_tools(), TOOLS, "Where is A100?")

    everything = str([dict(s.attributes) for s in spans()])
    assert "Where is A100?" not in everything
    assert "headphones" not in everything


async def test_each_model_calls_usage_lands_on_its_own_chat_span_and_the_totals_on_the_agent_span(spans):
    llm = FakeLLM([
        LLMResponse(
            tool_calls=[ToolCall(id="c1", name="get_order", arguments={"order_id": "A100"})],
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


async def test_a_model_that_reports_no_usage_leaves_the_spans_without_token_attributes(spans):
    await run_agent(model_that_calls_both_tools(), TOOLS, "Where is A100?")

    assert not any("usage" in key or key == "app.cost_usd" for s in spans() for key in s.attributes)


async def test_totals_are_recorded_when_the_run_ends_by_running_out_of_steps(spans):
    endless = FakeLLM([
        LLMResponse(
            tool_calls=[ToolCall(id=f"c{i}", name="get_order", arguments={"order_id": "A100"})],
            usage=Usage(input_tokens=100, output_tokens=10, cost_usd=0.01),
        )
        for i in range(3)
    ])

    await run_agent(endless, TOOLS, "Where is A100?", max_steps=2)

    assert named(spans, "invoke_agent").attributes["app.input_tokens"] == 200


async def test_a_crashing_tool_is_reported_to_sentry_once_with_its_name(spans, sentry_events):
    await run_agent(model_that_calls_both_tools(), TOOLS, "Where is A100?")

    [event] = sentry_events.events
    assert event["tags"]["tool.name"] == "search_policy"
    assert event["contexts"]["tool_call"]["arguments"] == {"order_id": "A100"}


async def test_tools_that_work_send_nothing_to_sentry(spans, sentry_events):
    llm = FakeLLM([
        LLMResponse(tool_calls=[ToolCall(id="c1", name="get_order", arguments={"order_id": "A100"})]),
        LLMResponse(text="Done."),
    ])

    await run_agent(llm, TOOLS, "Where is A100?")

    assert sentry_events.events == []
