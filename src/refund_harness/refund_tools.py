"""The refund agent's tools, backed by fake in-memory data for now.
Phase 2 replaces ORDERS with Postgres."""

from pydantic import BaseModel

from .tools import Tool, ToolRegistry

ORDERS = {
    "A100": {"item": "headphones", "total": 59.0, "status": "delivered", "days_since_delivery": 3},
    "A200": {"item": "desk lamp", "total": 35.0, "status": "delivered", "days_since_delivery": 45},
    "A300": {"item": "keyboard", "total": 89.0, "status": "shipped", "days_since_delivery": None},
}
POLICY = "Refunds are allowed within 30 days of delivery. Orders not yet delivered can be cancelled, not refunded."


class OrderArgs(BaseModel):
    order_id: str


class NoArgs(BaseModel):
    pass


async def get_order(args: OrderArgs) -> str:
    order = ORDERS.get(args.order_id)
    return str(order) if order else f"No order found with id {args.order_id}"


async def get_refund_policy(args: NoArgs) -> str:
    return POLICY


def build_registry() -> ToolRegistry:
    return ToolRegistry([
        Tool("get_order", "Look up an order by id.", OrderArgs, get_order),
        Tool("get_refund_policy", "Return the refund policy text.", NoArgs, get_refund_policy),
    ])
