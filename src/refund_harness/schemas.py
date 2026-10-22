"""HTTP request/response shapes: the API contract with clients, separate from the harness's internal types."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

from refund_harness.agent.types import Step, StopReason


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=1000)


class AskResponse(BaseModel):
    answer: str
    stop_reason: StopReason
    num_steps: int  # model calls (`steps` also lists each tool run)
    # Everything the agent did, for the operator's timeline. This API is for the business's own staff, behind a key.
    steps: list[Step]
    duration_ms: float
    input_tokens: int
    output_tokens: int
    cost_usd: float | None  # None if any call's cost is unknown


class ApproveResponse(BaseModel):
    refund_id: int
    status: Literal["approved"]


class RejectRequest(BaseModel):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)]  # stripped, then checked


class RejectResponse(BaseModel):
    refund_id: int
    status: Literal["rejected"]


class LinkStep(BaseModel):
    kind: Literal["card", "address", "customer"]
    label: str  # a card's last 4 digits, an address, or a customer's name


class LinkedCustomer(BaseModel):
    customer_id: str
    name: str | None
    refunds: int  # refund requests this customer has
    via: list[LinkStep]  # what lies between the two customers, in order


class RiskFlag(BaseModel):
    code: str
    message: str
    links: list[LinkedCustomer] | None = None  # on the linked-customers note: how each one is connected


RefundStatus = Literal["pending", "approved", "rejected", "issued"]


class RefundSummary(BaseModel):
    refund_id: int
    order_id: str
    item: str
    customer_id: str | None
    customer_name: str | None
    amount_cents: int
    order_total_cents: int
    status: RefundStatus
    created_at: datetime
    updated_at: datetime | None  # the latest audit event (None: nothing has happened since it was requested)
    risk_flags: list[RiskFlag] | None  # None: the linked-accounts check never ran; []: it ran and found nothing
    provider_refund_id: str | None  # the provider's payout id, once issued


class ActivityItem(BaseModel):
    at: datetime
    event: Literal["filed", "approved", "rejected", "issued"]
    actor: str  # "agent" for a filed request, the reviewer for a decision, "payout" for the worker
    note: str | None  # a rejection's reason
    refund_id: int
    amount_cents: int
    customer_name: str | None
    item: str
    flagged: bool  # filed with notes about linked accounts (not notes that the check could not run)
    provider_refund_id: str | None  # on a payout


class CustomerOrder(BaseModel):
    order_id: str
    item: str
    total_cents: int
    status: str
    delivered_at: datetime | None
    card_last4: str | None
    address: str | None
    refund_id: int | None  # the latest refund request on this order, if any
    refund_cents: int | None
    refund_status: RefundStatus | None


class CustomerDetail(BaseModel):
    customer_id: str
    name: str
    orders: list[CustomerOrder]


class RefundEvent(BaseModel):
    event: str
    actor: str
    note: str | None  # the reason, on a rejection
    created_at: datetime


class RefundDetail(RefundSummary):
    events: list[RefundEvent]


class LedgerLineOut(BaseModel):
    reference: str
    state: str
    ok: bool
    refund_id: int | None
    status: str | None
    amount_cents: int | None
    provider_refund_ids: list[str]
    provider_amount_cents: int


class Ledger(BaseModel):
    lines: list[LedgerLineOut]
    available: bool  # False: the provider could not be asked, so there is nothing to compare


class StatusTotal(BaseModel):
    count: int
    amount_cents: int


class QueueDepth(BaseModel):
    waiting: int
    in_flight: int


class OpsSummary(BaseModel):
    by_status: dict[str, StatusTotal]
    outbox_unsent: int
    outbox_oldest: datetime | None
    payouts_queue: QueueDepth | None  # None: the queue could not be asked
    dead_letters: QueueDepth | None
