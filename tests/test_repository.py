from refund_harness.refund_tools import build_registry
from refund_harness.repository import get_order_row


async def test_returns_the_merchants_own_order(pool):
    row = await get_order_row(pool, "acme", "A100")
    assert row["item"] == "headphones"
    assert row["total_cents"] == 5900


async def test_missing_order_returns_none(pool):
    row = await get_order_row(pool, '', '')
    assert row == None


async def test_other_merchants_order_is_invisible(pool):
    row = await get_order_row(pool, "globex", "A300")
    assert row is None          # the order exists in the table, but not for this merchant


async def test_harness_gets_order(pool):
    registry = build_registry(pool, "acme")
    result = await registry.get("get_order").run({"order_id": "A100"})
    assert "headphones" in result
