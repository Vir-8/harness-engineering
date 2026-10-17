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


class LLMResponse(BaseModel):
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)


StopReason = Literal["final", "max_steps"]


class RunResult(BaseModel):
    final_text: str
    stop_reason: StopReason
    messages: list[Message]

