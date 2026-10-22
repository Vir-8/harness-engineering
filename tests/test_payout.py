"""issue_refund: pays out an approved refund through the provider, then records it as issued.

The crash and two-payers tests are the reason for the idempotency key: without it, a crash after paying, or two payers at once,
paid the customer TWICE (both tests asserted 2 payouts before the key was added).
"""

import asyncio

import httpx
import pytest
from helpers import A100_TOTAL_CENTS, ACME, GLOBEX, approved_refund, payouts, request

from refund_harness.payments.client import PaymentProvider, ProviderRefund
from refund_harness.payments.payout import IssueOutcome, issue_refund


class Crash(Exception):
    """Stands in for the process dying. (The demo script really kills the process; in a test we can't.)"""


async def crash(paid: ProviderRefund) -> None:
    raise Crash


async def test_approved_refund_is_paid_and_issued(pool, refunds, provider):
    refund_id = await approved_refund(pool)

    result = await issue_refund(pool, provider, ACME, refund_id)

    assert result.outcome == IssueOutcome.ISSUED
    [payout] = await payouts(provider)
    assert payout["amount_cents"] == A100_TOTAL_CENTS
    assert payout["id"] == result.provider_refund_id
    assert await refunds.status_of(refund_id) == "issued"
    assert {"event": "issued", "actor": "payout"} in await refunds.events_for(refund_id)


async def already_paid(pool, provider) -> int:
    refund_id = await approved_refund(pool)
    await issue_refund(pool, provider, ACME, refund_id)
    return refund_id


async def not_approved_yet(pool, provider) -> int:
    return await request(pool, ACME, "A100", A100_TOTAL_CENTS)


async def approved(pool, provider) -> int:
    return await approved_refund(pool)


@pytest.mark.parametrize(
    ("make_refund", "payer", "status"),
    [(already_paid, ACME, "issued"), (not_approved_yet, ACME, "pending"), (approved, GLOBEX, "approved")],
    ids=["already_paid", "not_approved_yet", "another_merchant_pays"],
)
async def test_only_an_approved_unpaid_refund_is_paid(pool, refunds, provider, make_refund, payer, status):
    refund_id = await make_refund(pool, provider)
    ledger_before = await payouts(provider)

    result = await issue_refund(pool, provider, payer, refund_id)

    assert result.outcome == IssueOutcome.NOT_ISSUABLE
    assert await payouts(provider) == ledger_before
    assert await refunds.status_of(refund_id) == status  # untouched


async def test_retry_after_a_crash_pays_once(pool, refunds, provider):
    refund_id = await approved_refund(pool)
    with pytest.raises(Crash):
        await issue_refund(pool, provider, ACME, refund_id, after_payout=crash)
    assert await refunds.status_of(refund_id) == "approved"  # our database never heard about the payout

    retry = await issue_refund(pool, provider, ACME, refund_id)

    [payout] = await payouts(provider)  # the customer got the money once
    assert retry.replayed is True
    assert retry.provider_refund_id == payout["id"]  # and we recorded the payout that really happened
    assert await refunds.status_of(refund_id) == "issued"


async def test_two_workers_at_once_pay_once(pool, refunds, provider):
    refund_id = await approved_refund(pool)

    # Both read 'approved' before either records 'issued'; the provider sees the same key twice.
    results = await asyncio.gather(issue_refund(pool, provider, ACME, refund_id), issue_refund(pool, provider, ACME, refund_id))

    assert len(await payouts(provider)) == 1
    assert sorted(r.replayed for r in results) == [False, True]
    assert len([e for e in await refunds.events_for(refund_id) if e["event"] == "issued"]) == 1


async def test_every_payout_has_an_idempotency_key(pool, refunds):
    """The provider pays every keyless request, so OUR side must never send one."""
    sent = []

    def record(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"id": "re_1", "amount_cents": A100_TOTAL_CENTS, "reference": "r"})

    recording = PaymentProvider(httpx.AsyncClient(transport=httpx.MockTransport(record), base_url="http://provider"))
    refund_id = await approved_refund(pool)

    await issue_refund(pool, recording, ACME, refund_id)

    [request] = sent
    assert request.headers["Idempotency-Key"] == f"{ACME}:refund-{refund_id}"


# --- the provider's side of the key (the fake provider in this process, through our client) ---

async def test_same_key_returns_the_first_payout(provider):
    first = await provider.create_refund(4000, reference="acme:refund-1", idempotency_key="acme:refund-1")
    second = await provider.create_refund(4000, reference="acme:refund-1", idempotency_key="acme:refund-1")

    assert second.id == first.id
    assert (first.replayed, second.replayed) == (False, True)
    assert len(await payouts(provider)) == 1
