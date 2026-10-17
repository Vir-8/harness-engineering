import asyncio

from refund_harness.llm import FakeLLM
from refund_harness.loop import run_agent
from refund_harness.types import LLMResponse, ToolCall


def call(name: str, args: dict, id: str = "c1") -> LLMResponse:
    return LLMResponse(tool_calls=[ToolCall(id=id, name=name, arguments=args)])


# --- passing: the happy path works ------------------------------------------


async def test_tool_result_is_fed_back_and_loop_finishes(registry):
    llm = FakeLLM([call("get_order", {"order_id": "A100"}), LLMResponse(text="Order A100 was delivered")])
    result = await run_agent(llm, registry, "Where is order A100?")

    assert result.stop_reason == "final"
    assert result.final_text == "Order A100 was delivered"
    # the 2nd model call must have SEEN the tool output
    assert llm.calls[1][-1].role == "tool"
    assert llm.calls[1][-1].content == "delivered 3 days ago"


# --- YOUR TASK 1: tool failures are observations, not crashes ----------------
# The model made a mistake. The harness's job is to tell it what went wrong
# so it can retry, not to blow up the whole run.


async def test_unknown_tool_is_reported_to_model(registry):
    llm = FakeLLM([call("nope", {}), LLMResponse(text="sorry")])
    result = await run_agent(llm, registry, "Where is order A100?")

    tool_msg = llm.calls[1][-1]
    assert tool_msg.role == "tool" and tool_msg.tool_call_id == "c1"
    assert "unknown tool" in tool_msg.content.lower()
    assert result.stop_reason == "final"


async def test_invalid_arguments_are_reported_to_model(registry):
    llm = FakeLLM([call("get_order", {"id": "A100"}), LLMResponse(text="sorry")])
    result = await run_agent(llm, registry, "Where is order A100?")

    tool_msg = llm.calls[1][-1]
    assert "order_id" in tool_msg.content  # tells the model WHICH field was wrong
    assert result.stop_reason == "final"


async def test_tool_exception_is_reported_to_model(registry):
    llm = FakeLLM([call("get_order", {"order_id": "Z999"}), LLMResponse(text="sorry")])
    result = await run_agent(llm, registry, "Where is order A100?")

    assert llm.calls[1][-1].role == "tool"
    assert result.stop_reason == "final"


# --- YOUR TASK 2: a runaway model must not run forever -----------------------


class AlwaysCallsTool:
    async def complete(self, messages, tools):
        return call("get_order", {"order_id": "A100"})


async def test_max_steps_stops_a_runaway_loop(registry):
    # wait_for makes this test FAIL after 1s instead of hanging forever
    result = await asyncio.wait_for(
        run_agent(AlwaysCallsTool(), registry, "Where is order A100?", max_steps=5), timeout=1
    )
    assert result.stop_reason == "max_steps"
    assert sum(m.role == "assistant" for m in result.messages) == 5
