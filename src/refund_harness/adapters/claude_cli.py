"""Adapter: drives the `claude` CLI as a text-in/JSON-out model (our own tool-calling protocol)."""

import asyncio
import json
import uuid
from json import JSONDecodeError

from pydantic import ValidationError

from ..errors import ModelCallError, ModelTimeoutError, ReplyParseError
from ..types import LLMResponse, Message, ToolCall, ToolSpec

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


class ClaudeCLIClient:
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
                tool_calls = tool_calls
            )
        except (KeyError, JSONDecodeError, ValidationError, TypeError) as e:
            raise ReplyParseError("The model's reply was unusable") from e
        return response

    async def _run_claude(self, prompt: str, schema: dict, timeout: float = 120) -> str:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", "--output-format", "json", "--tools", "",
            "--json-schema", json.dumps(schema), "--no-session-persistence",
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