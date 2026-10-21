"""HTTP request/response shapes: the API contract with clients, separate from the harness's internal types."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from refund_harness.agent.types import StopReason


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=1000)


class AskResponse(BaseModel):
    answer: str
    stop_reason: StopReason
    num_steps: int


class ApproveResponse(BaseModel):
    refund_id: int
    status: Literal["approved"]


class RiskFlag(BaseModel):
    code: str
    message: str


class PendingRefund(BaseModel):
    refund_id: int
    order_id: str
    amount_cents: int
    created_at: datetime
    risk_flags: list[RiskFlag] | None  # None means the linked-accounts check never ran; [] means it ran and found nothing
