"""reject_refund and POST /refunds/{id}/reject: a reviewer turns a pending request down, with a reason in the audit log."""

import pytest
from helpers import A100_TOTAL_CENTS, ACME, GLOBEX, REVIEWER, approve, request

from refund_harness.data.repository import reject_refund

REASON = "Linked to two accounts with recent refunds."


async def reject(pool, refund_id: int, actor: str = "reviewer-1", merchant_id: str = ACME) -> bool:
    async with pool.connection() as conn:
        return await reject_refund(conn, merchant_id, refund_id, actor, REASON)


@pytest.mark.parametrize(
    ("decide_first", "decide_again", "final_status"),
    [(approve, reject, "approved"), (reject, approve, "rejected")],
    ids=["approved_cannot_be_rejected", "rejected_cannot_be_approved"],
)
async def test_decided_refund_cannot_be_decided_again(pool, refunds, decide_first, decide_again, final_status):
    refund_id = await request(pool, ACME, "A100", A100_TOTAL_CENTS)
    await decide_first(pool, refund_id)

    assert await decide_again(pool, refund_id) is False
    assert await refunds.status_of(refund_id) == final_status


async def test_order_can_be_refiled_after_a_rejection(pool, refunds):
    first = await request(pool, ACME, "A100", A100_TOTAL_CENTS)
    await reject(pool, first)

    second = await request(pool, ACME, "A100", A100_TOTAL_CENTS)

    assert second is not None and second != first


# --- the endpoint ---

async def test_rejection_records_reviewer_and_reason(client, pool, refunds):
    refund_id = await request(pool, ACME, "A100", A100_TOTAL_CENTS)

    response = await client.post(
        f"/refunds/{refund_id}/reject", json={"reason": "  Outside the return window.  ", "actor": "someone-else"}, headers=REVIEWER
    )

    assert response.status_code == 200
    assert response.json() == {"refund_id": refund_id, "status": "rejected"}
    detail = (await client.get(f"/refunds/{refund_id}", headers=REVIEWER)).json()
    assert detail["status"] == "rejected"
    # the reviewer comes from the header, never the body; the reason is stored trimmed
    assert [(e["event"], e["actor"], e["note"]) for e in detail["events"]] == [("rejected", "reviewer-1", "Outside the return window.")]


@pytest.mark.parametrize("body", [{}, {"reason": ""}, {"reason": "    "}], ids=["no_reason", "empty_reason", "only_spaces"])
async def test_rejection_needs_a_real_reason(client, pool, refunds, body):
    refund_id = await request(pool, ACME, "A100", A100_TOTAL_CENTS)

    response = await client.post(f"/refunds/{refund_id}/reject", json=body, headers=REVIEWER)

    assert response.status_code == 422
    assert await refunds.status_of(refund_id) == "pending"


async def test_other_merchant_gets_a_plain_404(client, pool, refunds):
    refund_id = await request(pool, ACME, "A100", A100_TOTAL_CENTS)

    response = await client.post(
        f"/refunds/{refund_id}/reject", json={"reason": REASON}, headers={**REVIEWER, "X-Merchant-Id": GLOBEX}
    )

    assert response.status_code == 404
    assert await refunds.status_of(refund_id) == "pending"
