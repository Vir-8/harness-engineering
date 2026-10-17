"""The refund agent's tools, backed by order data from Postgres."""

from datetime import datetime, timezone

from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from .repository import get_order_row
from .tools import Tool, ToolRegistry

POLICY = "Refunds are allowed within 30 days of delivery. Orders not yet delivered can be cancelled, not refunded."


class OrderArgs(BaseModel):
    order_id: str


class NoArgs(BaseModel):
    pass


async def get_refund_policy(args: NoArgs) -> str:
    return POLICY


def build_registry(pool: AsyncConnectionPool, merchant_id: str) -> ToolRegistry:
    async def get_order(args: OrderArgs) -> str:
        row = await get_order_row(pool, merchant_id, args.order_id)
        if row is None:
            return f"No order could be found with id: {args.order_id}."
        return format_order(args.order_id, row, datetime.now(timezone.utc))

    return ToolRegistry([
        Tool("get_order", "Look up an order by id.", OrderArgs, get_order),
        Tool("get_refund_policy", "Return the refund policy text.", NoArgs, get_refund_policy),
    ])


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
