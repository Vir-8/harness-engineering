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
    # assistant only: the provider's own record of this turn, handed back to the SAME adapter on the next call and
    # ignored by everything else (e.g. OpenAI's encrypted reasoning, which the model needs to continue its thinking).
    provider_items: list[dict] | None = None


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
    cache_write_1h_tokens: int = 0  # the part of cache_write_tokens written with the 1-hour lifetime (priced higher)
    cost_usd: float | None = None

    @property
    def model_name(self) -> str | None:
        """The model that answered if the provider said, else the one we asked for."""
        return self.model or self.request_model


class LLMResponse(BaseModel):
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage | None = None  # None when the provider did not say
    provider_items: list[dict] | None = None  # see Message.provider_items: the loop stores it on the assistant message


StopReason = Literal["final", "max_steps"]


class Step(BaseModel):
    """One thing the agent did, in order, with how long it took: what an operator's timeline shows."""

    kind: Literal["model", "tool"]
    name: str  # the model that answered, or the tool that ran
    duration_ms: float
    arguments: dict[str, Any] | None = None  # tool only
    output: str = ""  # the tool's result, or the model's text (empty when it only called tools)
    tool_calls: list[ToolCall] = Field(default_factory=list)  # model only: what it decided to call
    usage: Usage | None = None  # model only
    failed: bool = False  # tool only: it crashed, got invalid arguments, or does not exist


class RunResult(BaseModel):
    final_text: str
    stop_reason: StopReason
    messages: list[Message]
    steps: list[Step] = Field(default_factory=list)

