"""The seam between the harness and any model provider."""

from typing import Protocol

from .types import LLMResponse, Message, ToolSpec


class LLMClient(Protocol):
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


class RaisingLLM:
    """Always fails with the given exception. For testing error handling."""

    def __init__(self, exc: Exception):
        self.exc = exc

    async def complete(self, messages, tools):
        raise self.exc
