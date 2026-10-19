"""Adapter: drives the `claude` CLI as a text-in/JSON-out model (our own tool-calling protocol)."""

import asyncio
import json
import re
import uuid
from json import JSONDecodeError

from pydantic import ValidationError

from refund_harness.agent.errors import (
    ModelCallError,
    ModelTimeoutError,
    ReplyParseError,
)
from refund_harness.agent.pricing import estimate_cost
from refund_harness.agent.types import LLMResponse, Message, ToolCall, ToolSpec, Usage

REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "tool_calls": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "arguments": {"type": "object"}},
            "required": ["name", "arguments"],
        }},
    },
    "required": ["text", "tool_calls"],
}


# `claude -p` normally loads all of Claude Code around our prompt: its settings and memory files, skills, and every
# configured MCP server's tool list. None of that is wanted, and dropping it cuts a one-word reply from 34,927 input
# tokens ($0.12) to about 2,000.
#
# What must NOT be dropped is Claude Code's own system prompt. An earlier version replaced it (--system-prompt) and
# the evals showed why that is a mistake: this adapter gets structured replies through the CLI's structured-output
# mechanism, and the default prompt is what teaches the model to use it. With a replacement prompt Sonnet 5.5 often
# did not (the CLI gave up in 3 of 8 trials, and the model forgot to make its tool call in 5 of 8). With the default
# prompt kept: 1 give-up in 16 trials and the tool call made 15 times, at about a third of the cost of loading
# everything. Measured by running the same prompts repeatedly, not by reading the output of one.
LEAN_CONTEXT_FLAGS = [
    "--strict-mcp-config",  # no MCP servers (none are passed in, so none load)
    "--setting-sources", "",  # no user/project settings or memory files
    "--disable-slash-commands",  # no skills
]


# The CLI sometimes gives up on producing a structured reply ("error_max_structured_output_retries"): in the evals
# about 6% of single calls. It is a flaky step, not a real failure, so one more try is almost always enough. Other
# failures (a timeout, a bad login) are NOT retried: repeating them only costs time.
STRUCTURED_OUTPUT_GAVE_UP = "error_max_structured_output_retries"

# Pinned, not "whatever the CLI defaults to": the default changes over time, and comparing prompts or models
# fairly (evals) needs the model to stay put. The app lets CLAUDE_MODEL override it.
DEFAULT_MODEL = "claude-sonnet-5-5"


def _plain_model_name(name: str) -> str:
    """The CLI reports e.g. 'claude-opus-4-6[1m]' (the [1m] is the context-window variant); price lookups want the base name."""
    return re.sub(r"\[[^\]]*\]$", "", name)


class ClaudeCLIClient:
    def __init__(self, model: str = DEFAULT_MODEL, lean_context: bool = True, max_attempts: int = 2):
        self.model = model
        self.lean_context = lean_context  # False keeps all of Claude Code's own context: only for comparing the two
        self.max_attempts = max_attempts  # tries per call when the CLI gives up on structured output

    async def complete(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        prompt = self.build_prompt(messages, tools)
        raw_output = await self._run_reliably(prompt, REPLY_SCHEMA)

        output = self.parse_reply(raw_output)
        return output

    async def complete_structured(self, prompt: str, schema: dict) -> tuple[dict, Usage | None]:
        """One prompt in, one JSON object out (matching `schema`), plus what the call cost. For callers that are not
        the agent loop, such as the eval judge: no tools, no message history."""
        raw_output = await self._run_reliably(prompt, schema)
        try:
            envelope = json.loads(raw_output)
            structured = envelope["structured_output"]
            if not isinstance(structured, dict):
                raise TypeError("structured_output is not an object")
        except (KeyError, JSONDecodeError, TypeError) as e:
            raise ReplyParseError("The model's reply was unusable") from e
        return structured, self.parse_usage(envelope)

    def build_prompt(self, messages: list[Message], tools: list[ToolSpec]) -> str:
        history = json.dumps([m.model_dump() for m in messages], indent=2)
        tools_prompt = json.dumps([t.model_dump() for t in tools])
        prompt = f"Chat history: {history} \n Available tools are: {tools_prompt} \n To use a tool, put it in tool_calls. To give your final answer, put it in text and leave tool_calls empty."
        return prompt

    def parse_reply(self, raw_stdout: str) -> LLMResponse:
        try:
            json_object = json.loads(raw_stdout)
            tool_calls_json: list[dict] = json_object["structured_output"]["tool_calls"]
            tool_calls = [ToolCall(
                **tool_call,
                id = str(uuid.uuid4())
            ) for tool_call in tool_calls_json]
            response = LLMResponse(
                text = json_object["structured_output"]["text"],
                tool_calls = tool_calls,
                usage = self.parse_usage(json_object),
            )
        except (KeyError, JSONDecodeError, ValidationError, TypeError) as e:
            raise ReplyParseError("The model's reply was unusable") from e
        return response

    def parse_usage(self, envelope: dict) -> Usage | None:
        """The CLI reports uncached input, cache reads and cache writes separately; the standard wants one total.

        Never fails the reply: if the numbers are missing or odd, the answer still counts and usage is just unknown.
        """
        try:
            counts = envelope["usage"]
            cache_read = counts.get("cache_read_input_tokens", 0)
            cache_write = counts.get("cache_creation_input_tokens", 0)
            model = next(iter(envelope.get("modelUsage") or {}), None)
            breakdown = counts.get("cache_creation") or {}
            # When the CLI does not split cache writes by lifetime, assume the 1-hour kind: it is what every real reply used.
            cache_write_1h = breakdown.get("ephemeral_1h_input_tokens", cache_write)
            usage = Usage(
                provider="anthropic",
                request_model=self.model,
                model=_plain_model_name(model) if model else None,
                input_tokens=counts["input_tokens"] + cache_read + cache_write,
                output_tokens=counts["output_tokens"],
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
                cache_write_1h_tokens=cache_write_1h,
            )
            # NOT the CLI's own total_cost_usd: it ignores the model (see agent/pricing.py)
            return usage.model_copy(update={"cost_usd": estimate_cost(usage)})
        except (KeyError, TypeError, ValueError, ValidationError, AttributeError):
            return None

    async def _run_reliably(self, prompt: str, schema: dict) -> str:
        for attempt in range(1, self.max_attempts + 1):
            try:
                return await self._run_claude(prompt, schema)
            except ModelCallError as error:
                if STRUCTURED_OUTPUT_GAVE_UP not in str(error) or attempt == self.max_attempts:
                    raise
        raise AssertionError("unreachable: max_attempts must be at least 1")

    def _command(self, schema: dict) -> list[str]:
        return [
            "claude", "-p", "--output-format", "json", "--tools", "",
            "--json-schema", json.dumps(schema), "--no-session-persistence",
            "--model", self.model,
            *(LEAN_CONTEXT_FLAGS if self.lean_context else []),
        ]

    async def _run_claude(self, prompt: str, schema: dict, timeout: float = 120) -> str:
        proc = await asyncio.create_subprocess_exec(
            *self._command(schema),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        try:
            out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout)
        except asyncio.TimeoutError as e:
            proc.kill()
            await proc.wait()
            raise ModelTimeoutError("The model took too long, please try again later.") from e
        if proc.returncode != 0:
            # `claude -p --output-format json` reports many failures as JSON on stdout and leaves stderr empty
            detail = err.decode().strip() or out.decode().strip() or "(it printed nothing)"
            raise ModelCallError(f"claude exited {proc.returncode}: {detail[:500]}")
        return out.decode()