"""submit_refund: lock the order, check eligibility, insert the request, all in one transaction.

It returns a SubmitRefundResult whose `outcome` is one of: CREATED, DUPLICATE, INELIGIBLE, ORDER_NOT_FOUND.
"""

import asyncio

from helpers import A100_TOTAL_CENTS, ACME, DAY, GLOBEX, moment_after_delivery, submit
from refund_harness.repository import RefundOutcome


async def test_eligible_order_creates_a_pending_refund(pool, refunds):
    now = await moment_after_delivery(pool, 3 * DAY)

    result = await submit(pool, ACME, "A100", A100_TOTAL_CENTS, now)

    assert result.outcome == RefundOutcome.CREATED
    assert await refunds.status_of(result.refund_id) == "pending"
    assert await refunds.count() == 1


async def test_refund_above_the_total_is_ineligible_and_stores_nothing(pool, refunds):
    now = await moment_after_delivery(pool, 3 * DAY)

    result = await submit(pool, ACME, "A100", A100_TOTAL_CENTS + 1, now)

    assert result.outcome == RefundOutcome.INELIGIBLE
    assert any("Cannot refund more" in reason for reason in result.reasons)
    assert await refunds.count() == 0


async def test_refund_after_the_deadline_is_ineligible_and_stores_nothing(pool, refunds):
    now = await moment_after_delivery(pool, 45 * DAY)

    result = await submit(pool, ACME, "A100", A100_TOTAL_CENTS, now)

    assert result.outcome == RefundOutcome.INELIGIBLE
    assert any("over 30 days" in reason for reason in result.reasons)
    assert await refunds.count() == 0


async def test_submitting_the_same_order_twice_is_a_duplicate(pool, refunds):
    now = await moment_after_delivery(pool, 3 * DAY)

    first = await submit(pool, ACME, "A100", A100_TOTAL_CENTS, now)
    second = await submit(pool, ACME, "A100", A100_TOTAL_CENTS, now)

    assert first.outcome == RefundOutcome.CREATED
    assert second.outcome == RefundOutcome.DUPLICATE
    assert await refunds.count() == 1


async def test_simultaneous_submissions_create_only_one_refund(pool, refunds):
    now = await moment_after_delivery(pool, 3 * DAY)

    results = await asyncio.gather(
        submit(pool, ACME, "A100", A100_TOTAL_CENTS, now),
        submit(pool, ACME, "A100", A100_TOTAL_CENTS, now),
    )

    assert sorted(r.outcome for r in results) == [RefundOutcome.CREATED, RefundOutcome.DUPLICATE]
    assert await refunds.count() == 1


async def test_unknown_order_is_not_found(pool, refunds):
    now = await moment_after_delivery(pool, 3 * DAY)

    result = await submit(pool, ACME, "no-such-order", 100, now)

    assert result.outcome == RefundOutcome.ORDER_NOT_FOUND
    assert await refunds.count() == 0


async def test_another_merchants_order_is_not_found(pool, refunds):
    now = await moment_after_delivery(pool, 3 * DAY)

    # A300 exists, but it belongs to acme: for globex it must look exactly like a missing order
    result = await submit(pool, GLOBEX, "A300", 100, now)

    assert result.outcome == RefundOutcome.ORDER_NOT_FOUND
    assert await refunds.count() == 0
