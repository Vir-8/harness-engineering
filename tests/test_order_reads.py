"""get_order_row: reads one order, always scoped to one merchant."""

from helpers import A100_TOTAL_CENTS, ACME, GLOBEX

from refund_harness.data.repository import get_order_row


async def test_returns_the_merchants_own_order(pool):
    row = await get_order_row(pool, ACME, "A100")

    assert row["item"] == "headphones"
    assert row["total_cents"] == A100_TOTAL_CENTS


async def test_unknown_order_returns_none(pool):
    assert await get_order_row(pool, ACME, "no-such-order") is None


async def test_another_merchants_order_is_invisible(pool):
    # A300 exists, but it belongs to acme, so globex must not see it
    assert await get_order_row(pool, GLOBEX, "A300") is None
