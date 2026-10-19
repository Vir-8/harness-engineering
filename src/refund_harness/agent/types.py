"""Provider-neutral data shapes. Every LLM adapter translates to/from these."""

from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any]


class Message(BaseModel):
    role: Literal["user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)  # assistant only
    tool_call_id: str | None = None  # tool only: which call this answers


class ToolSpec(BaseModel):
    """What the model is told about a tool."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema


class Usage(BaseModel):
    """What one model call cost, in the shape OpenTelemetry and Sentry expect."""

    provider: str | None = None
    request_model: str | None = None  # the model we asked for
    model: str | None = None  # the model that answered, as the provider reports it
    input_tokens: int  # the TOTAL input, including the cached tokens below (a subset of it, not extra)
    output_tokens: int
    cache_read_tokens: int = 0  # input tokens served from the provider's cache
    cache_write_tokens: int = 0  # input tokens written to the provider's cache
    cost_usd: float | None = None


class LLMResponse(BaseModel):
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage | None = None  # None when the provider did not say


StopReason = Literal["final", "max_steps"]


class RunResult(BaseModel):
    final_text: str
    stop_reason: StopReason
    messages: list[Message]

