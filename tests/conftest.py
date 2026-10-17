import pytest
from pydantic import BaseModel

from refund_harness.tools import Tool, ToolRegistry

ORDERS = {"A100": "delivered 3 days ago"}


class OrderArgs(BaseModel):
    order_id: str


async def _get_order(args: OrderArgs) -> str:
    return ORDERS[args.order_id]  # KeyError for unknown orders, on purpose


def make_registry() -> ToolRegistry:
    return ToolRegistry(
        [Tool("get_order", "Look up an order's status by id.", OrderArgs, _get_order)]
    )

@pytest.fixture
def registry():
    return make_registry()
