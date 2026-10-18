"""approve_refund: moves a pending refund to approved AND records an audit event, all or nothing."""

import asyncio

import pytest
from helpers import A100_TOTAL_CENTS, ACME, GLOBEX, approve, request
from psycopg.errors import NotNullViolation


async def pending_refund(pool) -> int:
    return await request(pool, ACME, "A100", A100_TOTAL_CENTS)


async def test_approval_marks_the_refund_approved_and_logs_who_did_it(pool, refunds):
    refund_id = await pending_refund(pool)

    approved = await approve(pool, refund_id, actor="reviewer-1")

    assert approved is True
    assert await refunds.status_of(refund_id) == "approved"
    assert await refunds.events_for(refund_id) == [{"event": "approved", "actor": "reviewer-1"}]


async def test_two_simultaneous_approvals_succeed_only_once(pool, refunds):
    refund_id = await pending_refund(pool)

    # The second one waits for the first one's row lock, then finds the refund is no longer pending.
    results = await asyncio.gather(approve(pool, refund_id), approve(pool, refund_id))

    assert sorted(results) == [False, True]
    assert len(await refunds.events_for(refund_id)) == 1


async def test_approval_that_fails_halfway_changes_nothing(pool, refunds):
    refund_id = await pending_refund(pool)

    # actor=None breaks the audit insert, which runs AFTER the status update
    with pytest.raises(NotNullViolation):
        await approve(pool, refund_id, actor=None)

    assert await refunds.status_of(refund_id) == "pending"  # the status update was rolled back
    assert await refunds.events_for(refund_id) == []


async def test_another_merchant_cannot_approve_the_refund(pool, refunds):
    refund_id = await pending_refund(pool)

    approved = await approve(pool, refund_id, merchant_id=GLOBEX)

    assert approved is False
    assert await refunds.status_of(refund_id) == "pending"
