"""The Claude CLI adapter: our own tool-calling protocol over `claude -p`, its usage and cost, and its failures.

No test starts the real CLI: either the reply text is handed to the parser, or the subprocess is replaced.
"""

import json

import pytest

from refund_harness.adapters.claude_cli import LEAN_CONTEXT_FLAGS, ClaudeCLIClient
from refund_harness.agent.errors import ModelCallError, ReplyParseError
from refund_harness.agent.loop import run_agent
from refund_harness.agent.types import Message


def raw(text: str, tool_calls: list, **envelope) -> str:
    return json.dumps({"structured_output": {"text": text, "tool_calls": tool_calls}, **envelope})


# The numbers a real `claude -p` call reported for a one-word reply (see LEAN_CONTEXT_FLAGS for why they are so big).
REAL_USAGE = {
    "usage": {"input_tokens": 3, "cache_creation_input_tokens": 17499, "cache_read_input_tokens": 17425, "output_tokens": 60},
    "modelUsage": {"claude-opus-4-6[1m]": {"inputTokens": 3, "outputTokens": 60, "costUSD": 0.1196}},
    "total_cost_usd": 0.1196,
}


# --- reading the reply ---

def test_tool_calls_get_ids():
    reply = ClaudeCLIClient().parse_reply(raw("", [
        {"name": "get_order", "arguments": {"order_id": "A100"}},
        {"name": "get_order", "arguments": {"order_id": "A200"}},
    ]))

    assert [c.name for c in reply.tool_calls] == ["get_order", "get_order"]
    assert reply.tool_calls[0].arguments == {"order_id": "A100"}
    assert all(isinstance(c.id, str) and c.id for c in reply.tool_calls)
    assert reply.tool_calls[0].id != reply.tool_calls[1].id


def test_final_answer_parses_without_usage():
    reply = ClaudeCLIClient().parse_reply(raw("Order A100 was delivered", []))

    assert reply.text == "Order A100 was delivered"
    assert reply.tool_calls == []
    assert reply.usage is None


@pytest.mark.parametrize(
    "stdout",
    ["xyz", raw("", [{"arguments": {"order_id": "A100"}}]), raw("", ["xyz", "abc"])],
    ids=["not_json", "tool_call_without_a_name", "tool_calls_that_are_not_objects"],
)
def test_malformed_output_fails_cleanly(stdout):
    with pytest.raises(ReplyParseError):
        ClaudeCLIClient().parse_reply(stdout)


async def test_adapter_drives_the_loop(monkeypatch, registry):
    replies = [
        raw("Looking up order A100", [{"name": "get_order", "arguments": {"order_id": "A100"}}]),
        raw("Order A100 was delivered", []),
    ]
    prompts = []

    async def fake_run(self, prompt, schema):
        prompts.append(prompt)
        return replies.pop(0)

    monkeypatch.setattr(ClaudeCLIClient, "_run_claude", fake_run)
    result = await run_agent(ClaudeCLIClient(), registry, "Where is order A100?")

    assert result.stop_reason == "final"
    assert result.final_text == "Order A100 was delivered"
    assert len(prompts) == 2
    assert "delivered 3 days ago" in prompts[1]  # the tool's result went back to the model in the next prompt


# --- usage: tokens and cost ---

def test_usage_is_priced_from_tokens():
    usage = ClaudeCLIClient(model="claude-sonnet-5-5").parse_reply(raw("ok", [], **{**REAL_USAGE, "total_cost_usd": 9.99})).usage

    assert usage.input_tokens == 3 + 17499 + 17425  # the standard wants ONE total that includes the cached tokens
    assert usage.cache_read_tokens == 17425
    assert usage.cache_write_tokens == 17499
    assert usage.output_tokens == 60
    assert usage.provider == "anthropic"
    assert usage.request_model == "claude-sonnet-5-5"  # what we asked for...
    assert usage.model == "claude-opus-4-6"  # ...and what (this recorded reply) says answered, without the CLI's '[1m]'
    # From the tokens at Opus 4.6's published prices, never the CLI's own figure (9.99 here): it ignores the model.
    assert usage.cost_usd == pytest.approx(0.185218, abs=1e-6)


def test_odd_usage_never_fails_the_reply():
    reply = ClaudeCLIClient().parse_reply(raw("ok", [], usage={"input_tokens": "lots", "output_tokens": None}))

    assert reply.text == "ok"
    assert reply.usage is None


def test_cache_write_lifetime_is_read():
    envelope = {
        "usage": {"input_tokens": 6, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 0, "output_tokens": 10,
                  "cache_creation": {"ephemeral_5m_input_tokens": 600, "ephemeral_1h_input_tokens": 400}},
        "modelUsage": {"claude-sonnet-5-5": {}},
    }

    usage = ClaudeCLIClient().parse_reply(raw("ok", [], **envelope)).usage

    assert usage.cache_write_tokens == 1000 and usage.cache_write_1h_tokens == 400


# --- the command line ---

async def test_command_pins_model_and_drops_extra_context(monkeypatch):
    """The model is pinned, not left to the CLI's default, which changes over time and would make evals unfair.

    The lean flags are what turned 34,927 input tokens into 2,050 for the same one-word task.

    The default system prompt is kept because the structured reply mechanism depends on it. Replacing it looked like
    a free saving of tokens. In repeated trials the CLI then gave up on 3 of 8 calls and the model skipped its tool
    call on 5 of 8 (see the comment on LEAN_CONTEXT_FLAGS). Do not add --system-prompt without re-running the evals.
    """
    started: list[tuple] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self, prompt: bytes):
            return raw("ok", []).encode(), b""

    async def fake_exec(*argv, **kwargs):
        started.append(argv)
        return FakeProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    await ClaudeCLIClient(model="claude-haiku-5-5")._run_claude("hello", {"type": "object"})

    [argv] = started
    assert argv[argv.index("--model") + 1] == "claude-haiku-5-5"
    assert all(flag in argv for flag in LEAN_CONTEXT_FLAGS)
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--setting-sources") + 1] == ""  # an empty list of settings sources
    assert "--system-prompt" not in argv


# --- complete_structured: for callers that are not the agent loop (the eval judge) ---

async def test_structured_reply_comes_with_usage(monkeypatch):
    async def fake_run(self, prompt, schema):
        return json.dumps({"structured_output": {"supported": True}, **REAL_USAGE})

    monkeypatch.setattr(ClaudeCLIClient, "_run_claude", fake_run)

    answer, usage = await ClaudeCLIClient().complete_structured("grade this", {"type": "object"})

    assert answer == {"supported": True}
    assert usage.output_tokens == 60


@pytest.mark.parametrize("reply", ["not json", json.dumps({"no_structured_output": 1}), json.dumps({"structured_output": "text"})],
                         ids=["not_json", "missing_field", "not_an_object"])
async def test_unusable_structured_reply_is_refused(monkeypatch, reply):
    async def fake_run(self, prompt, schema):
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
async def test_cli_failure_reports_what_the_cli_said(monkeypatch, stdout, stderr, expected_in_message):
    async def fake_exec(*argv, **kwargs):
        return _FailingProcess(stdout, stderr)

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    with pytest.raises(ModelCallError, match=expected_in_message):
        await ClaudeCLIClient()._run_claude("hello", {"type": "object"})


# --- the CLI giving up on structured output is retried; other failures are not ---

GAVE_UP = ModelCallError('claude exited 1: {"type":"result","subtype":"error_max_structured_output_retries"}')


class _ScriptedCli:
    """Replaces _run_claude: raises or returns each scripted item in turn."""

    def __init__(self, monkeypatch, script):
        self.script, self.calls = list(script), 0
        monkeypatch.setattr(ClaudeCLIClient, "_run_claude", lambda client, prompt, schema: self())

    async def __call__(self):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


async def test_structured_output_give_up_is_retried_once(monkeypatch):
    cli = _ScriptedCli(monkeypatch, [GAVE_UP, raw("Hello.", [])])

    reply = await ClaudeCLIClient().complete([Message(role="user", content="hi")], [])

    assert reply.text == "Hello." and cli.calls == 2


@pytest.mark.parametrize(
    "failure, expected_calls",
    [(GAVE_UP, 2), (ModelCallError("claude exited 1: not logged in"), 1)],
    ids=["a_give_up_stops_after_the_allowed_attempts", "other_failures_are_not_retried"],
)
async def test_persistent_failure_is_raised_after_retries(monkeypatch, failure, expected_calls):
    cli = _ScriptedCli(monkeypatch, [failure, failure, raw("never reached", [])])

    with pytest.raises(ModelCallError) as raised:
        await ClaudeCLIClient(max_attempts=2).complete([Message(role="user", content="hi")], [])

    assert raised.value is failure
    assert cli.calls == expected_calls
