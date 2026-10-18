"""POST /refunds/{id}/approve: the human approval step. It is an HTTP endpoint, not a model tool.

The merchant comes from X-Merchant-Id and the approver from X-Reviewer-Id (stand-ins for real auth).
Anything that is not "a pending refund of this merchant" gets the same 404.
"""

import pytest
from helpers import A100_TOTAL_CENTS, ACME, GLOBEX, request

REVIEWER = {"X-Reviewer-Id": "reviewer-1"}


async def pending_refund(pool) -> int:
    return await request(pool, ACME, "A100", A100_TOTAL_CENTS)


async def test_approving_a_pending_refund_works_and_records_the_reviewer(client, pool, refunds):
    refund_id = await pending_refund(pool)

    response = await client.post(f"/refunds/{refund_id}/approve", headers=REVIEWER)

    assert response.status_code == 200
    assert response.json() == {"refund_id": refund_id, "status": "approved"}
    assert await refunds.status_of(refund_id) == "approved"
    assert await refunds.events_for(refund_id) == [{"event": "approved", "actor": "reviewer-1"}]


async def test_the_approver_comes_from_the_header_never_the_body(client, pool, refunds):
    refund_id = await pending_refund(pool)

    await client.post(f"/refunds/{refund_id}/approve", headers=REVIEWER, json={"actor": "someone-else"})

    assert await refunds.events_for(refund_id) == [{"event": "approved", "actor": "reviewer-1"}]


@pytest.mark.parametrize("headers", [{}, {"X-Reviewer-Id": ""}], ids=["missing_header", "empty_header"])
async def test_approval_without_a_reviewer_is_rejected_and_changes_nothing(client, pool, refunds, headers):
    refund_id = await pending_refund(pool)

    response = await client.post(f"/refunds/{refund_id}/approve", headers=headers)

    assert response.status_code == 422
    assert await refunds.status_of(refund_id) == "pending"
    assert await refunds.events_for(refund_id) == []


async def test_approving_twice_fails_the_second_time(client, pool, refunds):
    refund_id = await pending_refund(pool)

    first = await client.post(f"/refunds/{refund_id}/approve", headers=REVIEWER)
    second = await client.post(f"/refunds/{refund_id}/approve", headers=REVIEWER)

    assert first.status_code == 200
    assert second.status_code == 404
    assert len(await refunds.events_for(refund_id)) == 1  # one approval, one audit event


async def test_another_merchant_cannot_approve_and_learns_nothing(client, pool, refunds):
    refund_id = await pending_refund(pool)
    unknown_id = refund_id + 1000

    other_merchants = await client.post(
        f"/refunds/{refund_id}/approve", headers={**REVIEWER, "X-Merchant-Id": GLOBEX}
    )
    missing = await client.post(f"/refunds/{unknown_id}/approve", headers=REVIEWER)

    assert other_merchants.status_code == 404
    assert other_merchants.json() == missing.json()  # indistinguishable from a refund that does not exist
    assert await refunds.status_of(refund_id) == "pending"


@pytest.mark.parametrize("bad_id", [0, -1], ids=["zero", "negative"])
async def test_non_positive_refund_ids_are_rejected(client, refunds, bad_id):
    response = await client.post(f"/refunds/{bad_id}/approve", headers=REVIEWER)

    assert response.status_code == 422
