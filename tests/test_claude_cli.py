import json

import pytest

from refund_harness.adapters.claude_cli import LEAN_CONTEXT_FLAGS, ClaudeCLIClient
from refund_harness.errors import ReplyParseError
from refund_harness.loop import run_agent
from refund_harness.types import Message, ToolCall, ToolSpec


def raw(text: str, tool_calls: list, **envelope) -> str:
    return json.dumps({"structured_output": {"text": text, "tool_calls": tool_calls}, **envelope})


# The numbers a real `claude -p` call reported for a one-word reply (see LEAN_CONTEXT_FLAGS for why they are so big).
REAL_USAGE = {
    "usage": {"input_tokens": 3, "cache_creation_input_tokens": 17499, "cache_read_input_tokens": 17425, "output_tokens": 60},
    "modelUsage": {"claude-opus-4-6[1m]": {"inputTokens": 3, "outputTokens": 60, "costUSD": 0.1196}},
    "total_cost_usd": 0.1196,
}


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


# --- usage: tokens and cost ---

def test_usage_is_read_from_the_cli_reply_with_cached_tokens_inside_the_input_total():
    usage = ClaudeCLIClient().parse_reply(raw("ok", [], **REAL_USAGE)).usage

    assert usage.input_tokens == 3 + 17499 + 17425  # the standard wants ONE total that includes the cached tokens
    assert usage.cache_read_tokens == 17425
    assert usage.cache_write_tokens == 17499
    assert usage.output_tokens == 60
    assert usage.cost_usd == 0.1196
    assert usage.provider == "anthropic"


def test_the_model_name_loses_its_context_window_suffix():
    usage = ClaudeCLIClient().parse_reply(raw("ok", [], **REAL_USAGE)).usage

    assert usage.model == "claude-opus-4-6"  # the CLI says 'claude-opus-4-6[1m]'


def test_a_reply_without_usage_numbers_still_parses():
    reply = ClaudeCLIClient().parse_reply(raw("Order A100 was delivered", []))

    assert reply.text == "Order A100 was delivered"
    assert reply.usage is None


def test_odd_usage_numbers_never_fail_the_reply():
    reply = ClaudeCLIClient().parse_reply(raw("ok", [], usage={"input_tokens": "lots", "output_tokens": None}))

    assert reply.text == "ok"
    assert reply.usage is None


async def test_the_real_command_drops_claude_codes_own_context(monkeypatch):
    """The lean flags are what turned 34,927 input tokens into 2,050 for the same one-word task."""
    started: list[tuple] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self, prompt: bytes):
            return raw("ok", []).encode(), b""

    async def fake_exec(*argv, **kwargs):
        started.append(argv)
        return FakeProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    await ClaudeCLIClient()._run_claude("hello", {"type": "object"})

    [argv] = started
    assert all(flag in argv for flag in LEAN_CONTEXT_FLAGS)
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--setting-sources") + 1] == ""  # an empty list of settings sources


# --- the model is chosen, not left to the CLI's default ---

def test_the_default_model_is_pinned():
    assert ClaudeCLIClient().model == "claude-sonnet-5-5"


def test_the_command_names_the_model():
    command = ClaudeCLIClient(model="claude-haiku-5-5")._command({"type": "object"})

    assert command[command.index("--model") + 1] == "claude-haiku-5-5"


def test_usage_remembers_which_model_we_asked_for_and_which_one_answered():
    usage = ClaudeCLIClient(model="claude-sonnet-5-5").parse_reply(raw("ok", [], **REAL_USAGE)).usage

    assert usage.request_model == "claude-sonnet-5-5"
    assert usage.model == "claude-opus-4-6"  # what the (recorded) reply said answered
