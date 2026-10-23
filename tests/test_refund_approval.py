"""Approving a refund: POST /refunds/{id}/approve, the human step. It is an HTTP endpoint, never a model tool.

The merchant comes from X-Merchant-Id and the approver from X-Reviewer-Id (stand-ins for real auth). Anything that is not
"a pending refund of this merchant" gets the same 404. Under it, approve_refund moves the refund to approved, records an
audit event and queues the payout job, all or nothing (the all-or-nothing part is in test_outbox.py).
"""

import asyncio

import pytest
from helpers import A100_TOTAL_CENTS, ACME, GLOBEX, REVIEWER, approve, request


async def pending_refund(pool) -> int:
    return await request(pool, ACME, "A100", A100_TOTAL_CENTS)


async def test_reviewer_comes_from_the_header(client, pool, refunds):
    refund_id = await pending_refund(pool)

    response = await client.post(f"/refunds/{refund_id}/approve", headers=REVIEWER, json={"actor": "someone-else"})

    assert response.status_code == 200
    assert response.json() == {"refund_id": refund_id, "status": "approved"}
    assert await refunds.status_of(refund_id) == "approved"
    assert await refunds.events_for(refund_id) == [{"event": "approved", "actor": "reviewer-1"}]


@pytest.mark.parametrize("headers", [{}, {"X-Reviewer-Id": ""}], ids=["missing_header", "empty_header"])
async def test_approval_needs_a_reviewer(client, pool, refunds, headers):
    refund_id = await pending_refund(pool)

    response = await client.post(f"/refunds/{refund_id}/approve", headers=headers)

    assert response.status_code == 422
    assert await refunds.status_of(refund_id) == "pending"
    assert await refunds.events_for(refund_id) == []


async def test_other_merchant_cannot_approve(client, pool, refunds):
    refund_id = await pending_refund(pool)
    unknown_id = refund_id + 1000

    other_merchants = await client.post(
        f"/refunds/{refund_id}/approve", headers={**REVIEWER, "X-Merchant-Id": GLOBEX}
    )
    missing = await client.post(f"/refunds/{unknown_id}/approve", headers=REVIEWER)

    assert other_merchants.status_code == 404
    assert other_merchants.json() == missing.json()  # indistinguishable from a refund that does not exist
    assert await refunds.status_of(refund_id) == "pending"


async def test_simultaneous_approvals_succeed_once(pool, refunds):
    refund_id = await pending_refund(pool)

    # The second one waits for the first one's row lock, then finds the refund is no longer pending.
    results = await asyncio.gather(approve(pool, refund_id), approve(pool, refund_id))

    assert sorted(results) == [False, True]
    assert len(await refunds.events_for(refund_id)) == 1
