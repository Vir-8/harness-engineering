"""request_refund: at most one live refund request per order, enforced by the database.

A request counts as "live" unless it was rejected (the partial unique index in db/init/04).
"""

import asyncio

import pytest
from helpers import A100_TOTAL_CENTS, ACME, request

from refund_harness.data.repository import request_refund


async def test_a_second_request_for_the_same_order_returns_none(pool, refunds):
    first = await request(pool, ACME, "A100", A100_TOTAL_CENTS)
    second = await request(pool, ACME, "A100", A100_TOTAL_CENTS)

    assert isinstance(first, int)
    assert second is None
    assert await refunds.count() == 1


async def test_two_simultaneous_requests_create_only_one(pool, refunds):
    # Both see "no refund yet" at the same moment; only the unique index stops the second insert.
    results = await asyncio.gather(
        request(pool, ACME, "A100", A100_TOTAL_CENTS),
        request(pool, ACME, "A100", A100_TOTAL_CENTS),
    )

    new_ids = [r for r in results if r is not None]
    assert len(new_ids) == 1
    assert results.count(None) == 1
    assert await refunds.count() == 1


async def test_a_rejected_request_does_not_block_a_new_one(pool, refunds):
    first = await request(pool, ACME, "A100", A100_TOTAL_CENTS)
    await refunds.set_status(first, "rejected")

    second = await request(pool, ACME, "A100", A100_TOTAL_CENTS)

    assert isinstance(second, int) and second != first


async def test_request_refund_does_not_commit_on_its_own(pool, refunds):
    """The caller owns the transaction: if its later work fails, the insert must disappear too."""
    with pytest.raises(RuntimeError):
        async with pool.connection() as conn:
            async with conn.transaction():
                await request_refund(conn, ACME, "A100", A100_TOTAL_CENTS)
                raise RuntimeError("something later in the caller's work failed")

    assert await refunds.count() == 0
