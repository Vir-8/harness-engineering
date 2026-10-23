"""Filing a refund request: submit_refund locks the order, checks eligibility and inserts the request, in one transaction.

It returns a SubmitRefundResult whose `outcome` is one of: CREATED, DUPLICATE, INELIGIBLE, ORDER_NOT_FOUND.
Under it, insert_refund_request allows at most one live request per order, enforced by the database: a request counts as
"live" unless it was rejected (the partial unique index in db/init/04).
"""

import asyncio

import pytest
from helpers import (
    A100_TOTAL_CENTS,
    ACME,
    DAY,
    GLOBEX,
    moment_after_delivery,
    request,
    submit,
)

from refund_harness.data.repository import RefundOutcome, insert_refund_request


async def test_eligible_order_creates_a_pending_refund(pool, refunds):
    now = await moment_after_delivery(pool, 3 * DAY)

    result = await submit(pool, ACME, "A100", A100_TOTAL_CENTS, now)

    assert result.outcome == RefundOutcome.CREATED
    assert await refunds.status_of(result.refund_id) == "pending"
    assert await refunds.count() == 1


@pytest.mark.parametrize(
    ("merchant", "order", "amount", "days_after_delivery", "outcome", "reasons"),
    [
        (ACME, "A100", A100_TOTAL_CENTS + 1, 3, RefundOutcome.INELIGIBLE, ("Cannot refund more than order amount.",)),
        (ACME, "A100", A100_TOTAL_CENTS, 45, RefundOutcome.INELIGIBLE,
         ("It has been over 30 days since delivery, order ineligible for refund.",)),
        (ACME, "no-such-order", 100, 3, RefundOutcome.ORDER_NOT_FOUND, ()),
        # A300 exists, but it belongs to acme: for globex it must look exactly like a missing order
        (GLOBEX, "A300", 100, 3, RefundOutcome.ORDER_NOT_FOUND, ()),
    ],
    ids=["more_than_the_total", "after_the_deadline", "unknown_order", "another_merchants_order"],
)
async def test_refused_submission_stores_nothing(pool, refunds, merchant, order, amount, days_after_delivery, outcome, reasons):
    now = await moment_after_delivery(pool, days_after_delivery * DAY)

    result = await submit(pool, merchant, order, amount, now)

    assert (result.outcome, result.reasons) == (outcome, reasons)
    assert await refunds.count() == 0


async def test_same_order_twice_is_a_duplicate(pool, refunds):
    now = await moment_after_delivery(pool, 3 * DAY)

    first = await submit(pool, ACME, "A100", A100_TOTAL_CENTS, now)
    second = await submit(pool, ACME, "A100", A100_TOTAL_CENTS, now)

    assert first.outcome == RefundOutcome.CREATED
    assert second.outcome == RefundOutcome.DUPLICATE
    assert (second.refund_id, second.existing_status) == (first.refund_id, "pending")  # the request that blocked it
    assert await refunds.count() == 1


async def test_simultaneous_submissions_make_one_refund(pool, refunds):
    now = await moment_after_delivery(pool, 3 * DAY)

    results = await asyncio.gather(
        submit(pool, ACME, "A100", A100_TOTAL_CENTS, now),
        submit(pool, ACME, "A100", A100_TOTAL_CENTS, now),
    )

    assert sorted(r.outcome for r in results) == [RefundOutcome.CREATED, RefundOutcome.DUPLICATE]
    assert await refunds.count() == 1


async def test_submission_waits_for_the_order_lock(pool, refunds):
    """submit_refund locks the order row (FOR UPDATE), so a change to the order can't slip in between its check and its insert.

    The other connection takes the lock an ordinary UPDATE of the order takes (FOR NO KEY UPDATE). It is the right
    lock for this test: it conflicts with FOR UPDATE but NOT with the foreign-key check that the refund insert does
    anyway, so the submission only waits if submit_refund really asked for FOR UPDATE.
    """
    now = await moment_after_delivery(pool, 3 * DAY)

    async with pool.connection() as other_connection:
        await other_connection.execute(
            "SELECT 1 FROM orders WHERE merchant_id = %s AND id = 'A100' FOR NO KEY UPDATE", (ACME,)
        )
        submission = asyncio.create_task(submit(pool, ACME, "A100", A100_TOTAL_CENTS, now))

        finished, _ = await asyncio.wait({submission}, timeout=0.5)

        assert not finished  # still waiting for the other transaction
        assert await refunds.count() == 0
    # leaving the block ends the other transaction and releases the lock

    result = await asyncio.wait_for(submission, timeout=5)
    assert result.outcome == RefundOutcome.CREATED


# --- insert_refund_request: the database's own guard, below the order lock ---

async def test_unique_index_stops_a_second_request(pool, refunds):
    # Both see "no refund yet" at the same moment; only the unique index stops the second insert.
    results = await asyncio.gather(
        request(pool, ACME, "A100", A100_TOTAL_CENTS),
        request(pool, ACME, "A100", A100_TOTAL_CENTS),
    )

    assert results.count(None) == 1  # the other one got an id
    assert await refunds.count() == 1


async def test_insert_leaves_the_commit_to_the_caller(pool, refunds):
    """The caller owns the transaction: if its later work fails, the insert must disappear too."""
    with pytest.raises(RuntimeError):
        async with pool.connection() as conn, conn.transaction():
            await insert_refund_request(conn, ACME, "A100", A100_TOTAL_CENTS)
            raise RuntimeError("something later in the caller's work failed")

    assert await refunds.count() == 0
