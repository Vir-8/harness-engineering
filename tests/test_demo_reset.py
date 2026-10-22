"""The demo data (refund_harness/demo.py): what the console's "Reset demo data" button and scripts/seed_demo.py rebuild."""

import pytest

from refund_harness import demo


@pytest.fixture
async def demo_rows(pool, refunds):
    """Take the demo rows out again afterwards: other tests expect exactly the seed data of db/init."""
    yield
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("TRUNCATE refund_requests, refund_events, outbox")
        await conn.execute("DELETE FROM orders WHERE id ~ '^(C1|G1)[0-9]{2}$'")
        for table, key in (("customers", "customer_id"), ("payment_methods", "payment_method_id"), ("addresses", "shipping_address_id")):
            await conn.execute(
                f"DELETE FROM {table} t WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.merchant_id = t.merchant_id AND o.{key} = t.id)"
            )


async def statuses(pool) -> dict[str, int]:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT status, count(*) AS n FROM refund_requests GROUP BY status")
        return {row["status"]: row["n"] for row in await cur.fetchall()}


async def test_reset_rebuilds_the_same_demo(pool, provider, demo_rows):
    # A payout left over from before: it would match no refund and show on the ledger as an exception.
    await provider.create_refund(100, "acme:refund-999", idempotency_key="acme:refund-999")

    # No worker runs against the test database, so the approved requests stay approved (unpaid) here.
    result = await demo.reset_demo(pool, None, provider, payout_wait_s=0)

    approved = sum(1 for *_, approve_after in demo.REQUESTS if approve_after is not None)
    assert result.requests == len(demo.REQUESTS) and result.unpaid == approved  # every request went through the real code
    first = await statuses(pool)
    assert first == {
        "approved": approved,
        "rejected": len(demo.REJECTED),
        "pending": len(demo.REQUESTS) - approved - len(demo.REJECTED),
    }
    assert await provider.list_refunds() == []  # the provider's ledger was emptied first

    await demo.reset_demo(pool, None, provider, payout_wait_s=0)

    assert await statuses(pool) == first  # running it again gives the same demo

