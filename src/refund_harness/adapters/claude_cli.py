"""Adapter: drives the `claude` CLI as a text-in/JSON-out model (our own tool-calling protocol)."""

import asyncio
import json
import re
import uuid
from json import JSONDecodeError

from pydantic import ValidationError

from ..errors import ModelCallError, ModelTimeoutError, ReplyParseError
from ..types import LLMResponse, Message, ToolCall, ToolSpec, Usage

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


# `claude -p` normally loads all of Claude Code around our prompt: its default system prompt, settings and memory
# files, and every configured MCP server's tool list. Measured on a one-word reply, that was 34,927 input tokens
# and $0.12; with the flags below it is 2,050 tokens and $0.012, with the same answer. Our own prompt carries
# everything the model needs (see build_prompt), so none of that context is wanted.
LEAN_CONTEXT_FLAGS = [
    "--strict-mcp-config",  # no MCP servers (none are passed in, so none load)
    "--setting-sources", "",  # no user/project settings or memory files
    "--disable-slash-commands",  # no skills
    "--system-prompt", "You are the model inside a tool-using agent. Follow the instructions in the message and reply only in the requested JSON shape.",
]


# Pinned, not "whatever the CLI defaults to": the default changes over time, and comparing prompts or models
# fairly (evals) needs the model to stay put. The app lets CLAUDE_MODEL override it.
DEFAULT_MODEL = "claude-sonnet-5-5"


def _plain_model_name(name: str) -> str:
    """The CLI reports e.g. 'claude-opus-4-6[1m]' (the [1m] is the context-window variant); price lookups want the base name."""
    return re.sub(r"\[[^\]]*\]$", "", name)


class ClaudeCLIClient:
    def __init__(self, model: str = DEFAULT_MODEL):
        self.model = model

    async def complete(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        prompt = self.build_prompt(messages, tools)
        raw_output = await self._run_claude(prompt, REPLY_SCHEMA)

        output = self.parse_reply(raw_output)
        return output

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
            return Usage(
                provider="anthropic",
                request_model=self.model,
                model=_plain_model_name(model) if model else None,
                input_tokens=counts["input_tokens"] + cache_read + cache_write,
                output_tokens=counts["output_tokens"],
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
                cost_usd=envelope.get("total_cost_usd"),
            )
        except (KeyError, TypeError, ValueError, ValidationError, AttributeError):
            return None

    def _command(self, schema: dict) -> list[str]:
        return [
            "claude", "-p", "--output-format", "json", "--tools", "",
            "--json-schema", json.dumps(schema), "--no-session-persistence",
            "--model", self.model,
            *LEAN_CONTEXT_FLAGS,
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
            raise ModelCallError(f"claude exited {proc.returncode}: {err.decode()[:500]}")
        return out.decode()