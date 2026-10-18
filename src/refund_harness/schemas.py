"""HTTP request/response shapes: the API contract with clients, separate from the harness's internal types."""

from typing import Literal

from pydantic import BaseModel, Field

from .types import StopReason


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=1000)


class AskResponse(BaseModel):
    answer: str
    stop_reason: StopReason
    num_steps: int


class ApproveResponse(BaseModel):
    refund_id: int
    status: Literal["approved"]
