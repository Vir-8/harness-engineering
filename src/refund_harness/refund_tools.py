"""The refund agent's tools, backed by order data from Postgres."""

from datetime import datetime, timezone
from typing import Callable

from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

from .repository import RefundOutcome, SubmitRefundResult, get_order_row, submit_refund
from .tools import Tool, ToolRegistry

POLICY = "Refunds are allowed within 30 days of delivery. Orders not yet delivered can be cancelled, not refunded."


class OrderArgs(BaseModel):
    order_id: str


class RefundArgs(BaseModel):
    order_id: str
    amount_cents: int = Field(gt=0, description="Refund amount in CENTS (5900 means $59.00), not dollars.")


class NoArgs(BaseModel):
    pass


async def get_refund_policy(args: NoArgs) -> str:
    return POLICY


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def build_registry(
    pool: AsyncConnectionPool, merchant_id: str, now: Callable[[], datetime] = _utc_now
) -> ToolRegistry:
    """`now` is the clock: the real one in production, a fixed one in tests."""

    async def get_order(args: OrderArgs) -> str:
        row = await get_order_row(pool, merchant_id, args.order_id)
        if row is None:
            return order_not_found_text(args.order_id)
        return format_order(args.order_id, row, now())

    async def submit_order_refund(args: RefundArgs) -> str:
        async with pool.connection() as conn:
            result = await submit_refund(conn, merchant_id, args.order_id, args.amount_cents, now())
        return format_refund_result(args.order_id, args.amount_cents, result)

    return ToolRegistry([
        Tool("get_order", "Look up an order by id.", OrderArgs, get_order),
        Tool("get_refund_policy", "Return the refund policy text.", NoArgs, get_refund_policy),
        Tool(
            "submit_order_refund",
            "Submit a refund request for an order. This only REQUESTS the refund: it stays pending until a human approves it.",
            RefundArgs,
            submit_order_refund,
        ),
    ])


def order_not_found_text(order_id: str) -> str:
    """One wording for 'no such order', whether it never existed or belongs to another merchant."""
    return f"No order could be found with id: {order_id}."


def format_refund_result(order_id: str, amount_cents: int, result: SubmitRefundResult) -> str:
    match result.outcome:
        case RefundOutcome.CREATED:
            return (
                f"Refund request {result.refund_id} for order {order_id} (${_dollars(amount_cents)}) was submitted "
                "and is pending human approval. No money has been refunded yet."
            )
        case RefundOutcome.DUPLICATE:
            return f"A refund request for order {order_id} already exists, so no new request was created."
        case RefundOutcome.INELIGIBLE:
            reasons = "\n".join(f"- {r}" for r in result.reasons)
            return f"Order {order_id} is not eligible for this refund:\n{reasons}"
        case RefundOutcome.ORDER_NOT_FOUND:
            return order_not_found_text(order_id)


def _dollars(cents: int) -> str:
    dollars, rest = divmod(cents, 100)
    return f"{dollars}.{rest:02d}"


def format_order(order_id: str, row: dict, now: datetime) -> str:
    dollars, cents = divmod(row["total_cents"], 100)
    delivered_at = row["delivered_at"]
    if delivered_at is None:
        delivery = "not delivered yet"
    else:
        days = (now - delivered_at).days
        delivery = f"{days} day{'' if days == 1 else 's'} ago ({delivered_at.date().isoformat()})"
    return (
        f"Order {order_id}: {row['item']}\n"
        f"Status: {row['status']}\n"
        f"Total: ${dollars}.{cents:02d}\n"
        f"Delivered: {delivery}"
    )
