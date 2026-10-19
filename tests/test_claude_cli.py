import json

import pytest

from refund_harness.adapters.claude_cli import LEAN_CONTEXT_FLAGS, ClaudeCLIClient
from refund_harness.agent.errors import ModelCallError, ReplyParseError
from refund_harness.agent.loop import run_agent
from refund_harness.agent.types import Message, ToolCall, ToolSpec


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
    assert usage.cost_usd == pytest.approx(0.185218, abs=1e-6)  # computed from the tokens at Opus 4.6's published prices
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


# --- complete_structured: for callers that are not the agent loop (the eval judge) ---

async def test_structured_completion_returns_the_object_and_its_usage(monkeypatch):
    async def fake_run(self, prompt, schema, timeout=120):
        return json.dumps({"structured_output": {"supported": True}, **REAL_USAGE})

    monkeypatch.setattr(ClaudeCLIClient, "_run_claude", fake_run)

    answer, usage = await ClaudeCLIClient().complete_structured("grade this", {"type": "object"})

    assert answer == {"supported": True}
    assert usage.output_tokens == 60


@pytest.mark.parametrize("reply", ["not json", json.dumps({"no_structured_output": 1}), json.dumps({"structured_output": "text"})],
                         ids=["not_json", "missing_field", "not_an_object"])
async def test_structured_completion_refuses_an_unusable_reply(monkeypatch, reply):
    async def fake_run(self, prompt, schema, timeout=120):
        return reply

    monkeypatch.setattr(ClaudeCLIClient, "_run_claude", fake_run)

    with pytest.raises(ReplyParseError):
        await ClaudeCLIClient().complete_structured("grade this", {"type": "object"})


# --- when the CLI fails, say what it said ---

class _FailingProcess:
    def __init__(self, stdout: bytes, stderr: bytes, code: int = 1):
        self.returncode, self._stdout, self._stderr = code, stdout, stderr

    async def communicate(self, prompt: bytes):
        return self._stdout, self._stderr


@pytest.mark.parametrize(
    "stdout, stderr, expected_in_message",
    [
        (b'{"is_error": true, "result": "usage limit reached"}', b"", "usage limit reached"),  # the CLI's JSON error on stdout
        (b"", b"authentication failed", "authentication failed"),
        (b"", b"", "printed nothing"),
    ],
    ids=["error_on_stdout", "error_on_stderr", "nothing_at_all"],
)
async def test_a_failed_cli_call_reports_what_the_cli_said(monkeypatch, stdout, stderr, expected_in_message):
    async def fake_exec(*argv, **kwargs):
        return _FailingProcess(stdout, stderr)

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    with pytest.raises(ModelCallError, match=expected_in_message):
        await ClaudeCLIClient()._run_claude("hello", {"type": "object"})


def test_the_default_system_prompt_is_kept_because_the_structured_reply_mechanism_depends_on_it():
    """Replacing it looked like a free saving of tokens. In repeated trials the CLI then gave up on 3 of 8 calls and the
    model skipped its tool call on 5 of 8 (see the comment on LEAN_CONTEXT_FLAGS). Do not re-add these without re-running the evals."""
    assert "--system-prompt" not in LEAN_CONTEXT_FLAGS


def test_leaving_lean_mode_drops_all_the_lean_flags():
    command = ClaudeCLIClient(lean_context=False)._command({"type": "object"})

    assert not any(flag in command for flag in ("--strict-mcp-config", "--setting-sources", "--disable-slash-commands"))


# --- the CLI giving up on structured output is retried; other failures are not ---

GAVE_UP = ModelCallError('claude exited 1: {"type":"result","subtype":"error_max_structured_output_retries"}')


class _ScriptedCli:
    """Replaces _run_claude: raises or returns each scripted item in turn."""

    def __init__(self, script):
        self.script, self.calls = list(script), 0

    async def __call__(self, prompt, schema, timeout=120):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


async def test_a_cli_give_up_on_structured_output_is_retried_once(monkeypatch):
    cli = _ScriptedCli([GAVE_UP, raw("Hello.", [])])
    monkeypatch.setattr(ClaudeCLIClient, "_run_claude", lambda self, prompt, schema, timeout=120: cli(prompt, schema))

    reply = await ClaudeCLIClient().complete([Message(role="user", content="hi")], [])

    assert reply.text == "Hello." and cli.calls == 2


async def test_it_gives_up_after_the_allowed_attempts(monkeypatch):
    cli = _ScriptedCli([GAVE_UP, GAVE_UP, raw("never reached", [])])
    monkeypatch.setattr(ClaudeCLIClient, "_run_claude", lambda self, prompt, schema, timeout=120: cli(prompt, schema))

    with pytest.raises(ModelCallError, match="error_max_structured_output_retries"):
        await ClaudeCLIClient(max_attempts=2).complete([Message(role="user", content="hi")], [])

    assert cli.calls == 2


async def test_other_failures_are_not_retried(monkeypatch):
    cli = _ScriptedCli([ModelCallError("claude exited 1: not logged in"), raw("never reached", [])])
    monkeypatch.setattr(ClaudeCLIClient, "_run_claude", lambda self, prompt, schema, timeout=120: cli(prompt, schema))

    with pytest.raises(ModelCallError, match="not logged in"):
        await ClaudeCLIClient().complete([Message(role="user", content="hi")], [])

    assert cli.calls == 1


async def test_structured_completions_get_the_same_retry(monkeypatch):
    cli = _ScriptedCli([GAVE_UP, json.dumps({"structured_output": {"supported": True}})])
    monkeypatch.setattr(ClaudeCLIClient, "_run_claude", lambda self, prompt, schema, timeout=120: cli(prompt, schema))

    answer, _ = await ClaudeCLIClient().complete_structured("grade this", {"type": "object"})

    assert answer == {"supported": True} and cli.calls == 2


# --- cost: from the tokens and published prices, never the CLI's own figure ---

def test_the_clis_own_cost_is_ignored_because_it_is_not_model_aware():
    reply = ClaudeCLIClient().parse_reply(raw("ok", [], **{**REAL_USAGE, "total_cost_usd": 9.99}))

    assert reply.usage.cost_usd != 9.99
    assert reply.usage.cost_usd == pytest.approx(0.185218, abs=1e-6)


def test_the_cache_write_lifetime_is_read_from_the_clis_breakdown():
    envelope = {
        "usage": {"input_tokens": 6, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 0, "output_tokens": 10,
                  "cache_creation": {"ephemeral_5m_input_tokens": 600, "ephemeral_1h_input_tokens": 400}},
        "modelUsage": {"claude-sonnet-5-5": {}},
    }

    usage = ClaudeCLIClient().parse_reply(raw("ok", [], **envelope)).usage

    assert usage.cache_write_tokens == 1000 and usage.cache_write_1h_tokens == 400


def test_an_unknown_model_gives_an_unknown_cost_but_the_reply_still_parses():
    envelope = {**REAL_USAGE, "modelUsage": {"claude-mystery-9": {}}}

    reply = ClaudeCLIClient().parse_reply(raw("ok", [], **envelope))

    assert reply.text == "ok" and reply.usage.cost_usd is None and reply.usage.output_tokens == 60
