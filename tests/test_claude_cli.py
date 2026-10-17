import json

import pytest

from refund_harness.adapters.claude_cli import ClaudeCLIClient
from refund_harness.errors import ReplyParseError
from refund_harness.loop import run_agent
from refund_harness.types import Message, ToolCall, ToolSpec


def raw(text: str, tool_calls: list) -> str:
    return json.dumps({"structured_output": {"text": text, "tool_calls": tool_calls}})


def test_parse_reply_turns_tool_calls_into_toolcalls_with_ids():
    reply = ClaudeCLIClient().parse_reply(raw("", [
        {"name": "get_order", "arguments": {"order_id": "A100"}},
        {"name": "get_order", "arguments": {"order_id": "A200"}},
    ]))

    assert [c.name for c in reply.tool_calls] == ["get_order", "get_order"]
    assert reply.tool_calls[0].arguments == {"order_id": "A100"}
    assert all(isinstance(c.id, str) and c.id for c in reply.tool_calls)
    assert reply.tool_calls[0].id != reply.tool_calls[1].id


def test_final_answer_parses_correctly():
    reply = ClaudeCLIClient().parse_reply(raw("Order A100 was delivered", []))
    assert(reply.text == "Order A100 was delivered")
    assert(reply.tool_calls == [])


def test_malformed_output_fails_cleanly():
    with pytest.raises(ReplyParseError):
        ClaudeCLIClient().parse_reply("xyz")

    with pytest.raises(ReplyParseError):
        ClaudeCLIClient().parse_reply(raw("", [
        {"arguments": {"order_id": "A100"}},
        {"name": "", "arguments": {"order_id": "A200"}},
    ]))

    with pytest.raises(ReplyParseError):
        ClaudeCLIClient().parse_reply(raw("", [
        "xyz",
        "abc"
    ]))


def test_build_prompt_keeps_context():
    history = [
        Message(role="user", content="Where is order A100?"),
        Message(role="assistant", tool_calls=[ToolCall(id="c1", name="get_order", arguments={"order_id": "A100"})]),
        Message(role="tool", content="delivered 3 days ago", tool_call_id="c1"),
    ]
    tools = [ToolSpec(name="get_order", description="Look up an order's status by id.", parameters={"type": "object"})]
    prompt = ClaudeCLIClient().build_prompt(history, tools)

    assert "c1" in prompt
    assert "get_order" in prompt
    assert "delivered 3 days ago" in prompt
    assert "leave tool_calls empty" in prompt


async def test_adapter_drives_the_loop(monkeypatch, registry):
    replies = [
        raw("Looking up order A100", [{"name": "get_order", "arguments": {"order_id": "A100"}}]),
        raw("Order A100 was delivered", [])
    ]

    prompts = []

    async def fake_run(self, prompt, schema, timeout=120):
        prompts.append(prompt)
        return replies.pop(0)

    monkeypatch.setattr(ClaudeCLIClient, "_run_claude", fake_run)
    result = await run_agent(ClaudeCLIClient(), registry, "Where is order A100?", max_steps=5)

    assert result.stop_reason == "final"
    assert result.final_text == "Order A100 was delivered"
    assert len(prompts) == 2
    assert "delivered 3 days ago" in prompts[1]