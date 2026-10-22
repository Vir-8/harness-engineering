"""The seam between the harness and any model provider."""

import asyncio
import re
from typing import Protocol

from refund_harness.agent.types import LLMResponse, Message, ToolCall, ToolSpec, Usage


class LLMClient(Protocol):
    """Turns the conversation so far into the model's next turn. Adapters raise agent.errors types, never their SDK's own."""

    async def complete(
        self, messages: list[Message], tools: list[ToolSpec]
    ) -> LLMResponse: ...


class FakeLLM:
    """Replays a scripted list of responses. Lets us test the harness
    deterministically, with no network and no cost."""

    def __init__(self, script: list[LLMResponse]):
        self._script = list(script)
        self.calls: list[list[Message]] = []  # what the model was shown, per turn

    async def complete(
        self, messages: list[Message], tools: list[ToolSpec]
    ) -> LLMResponse:
        self.calls.append(list(messages))
        if not self._script:
            raise AssertionError("FakeLLM script exhausted: harness called the model more than expected")
        return self._script.pop(0)


class LoadTestLLM:
    """A stand-in model for load tests (LLM_PROVIDER=fake): it waits like a real model, then behaves like a short run.

    First call: look up the order named in the question (a real tool call, so the database does real work).
    Second call: a final answer. It reports token usage of a typical call, but no cost: nothing was spent.
    Load tests then measure OUR harness (HTTP, database, Redis, the loop) under load, not the model provider.
    """

    MODEL = "load-test-fake"

    def __init__(self, delay_s: float = 1.0):
        self.delay_s = delay_s

    async def complete(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        await asyncio.sleep(self.delay_s)  # asyncio.sleep, not time.sleep: a real model call also leaves the loop free
        usage = Usage(provider="fake", model=self.MODEL, input_tokens=2000, output_tokens=150)
        if messages[-1].role == "user":
            order = re.search(r"\b[A-Z]\d{3}\b", messages[-1].content)
            call = ToolCall(id="call-1", name="get_order", arguments={"order_id": order.group() if order else "A100"})
            return LLMResponse(tool_calls=[call], usage=usage)
        return LLMResponse(text="Thanks, I have looked up your order.", usage=usage)
