"""Paying out an approved refund: approved -> (money sent by the provider) -> issued.

Three steps, and they CANNOT be one transaction, because the middle one happens in someone else's system:

    1. read the refund (Postgres): is it approved? how much?
    2. ask the provider to send the money          <- outside our database: no rollback possible
    3. record it (Postgres): status 'issued', the provider's id, an audit event

A crash between 2 and 3 leaves the money sent but the refund still 'approved', so the next run asks the provider again.
That is safe only because step 2 sends an idempotency key made from the refund's id: the provider recognises the retry
and returns the first payout instead of making a second one. The key, not our database, prevents the double.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from psycopg_pool import AsyncConnectionPool

from refund_harness.data.repository import get_approved_refund, mark_refund_issued
from refund_harness.payments.client import PaymentProvider, ProviderRefund

PAYOUT_ACTOR = "payout"  # who the audit log says issued it: the system, not a person


class IssueOutcome(StrEnum):
    ISSUED = "issued"
    NOT_ISSUABLE = "not_issuable"  # no such refund for this merchant, or not in 'approved' (pending, already issued...)


@dataclass(frozen=True)
class IssueResult:
    outcome: IssueOutcome
    provider_refund_id: str | None = None
    replayed: bool = False  # the provider had already paid this refund (an earlier attempt crashed or timed out)


def payout_key_prefix(merchant_id: str) -> str:
    """What every one of this merchant's payout keys starts with."""
    return f"{merchant_id}:refund-"


def payout_key(merchant_id: str, refund_id: int) -> str:
    """Names THIS refund's payout: the same on every attempt, different for every refund (refund ids never repeat).

    It is both the idempotency key and the reference that shows on the provider's ledger.
    """
    return f"{payout_key_prefix(merchant_id)}{refund_id}"


async def issue_refund(
    pool: AsyncConnectionPool,
    provider: PaymentProvider,
    merchant_id: str,
    refund_id: int,
    *,
    after_payout: Callable[[ProviderRefund], Awaitable[None]] | None = None,
) -> IssueResult:
    """Pay out one approved refund and record it. Safe to retry after any failure, crash included.

    `after_payout` runs between the payout and the record: the tests, and the worker's --crash-after-payout drill, use
    it to crash right there.
    """
    refund = await get_approved_refund(pool, merchant_id, refund_id)
    if refund is None:
        return IssueResult(IssueOutcome.NOT_ISSUABLE)

    # No database transaction or row lock is held during this call on purpose: the provider can take seconds,
    # and a lock held that long would block every other query on this row.
    key = payout_key(merchant_id, refund_id)
    paid = await provider.create_refund(refund["amount_cents"], reference=key, idempotency_key=key)

    if after_payout is not None:
        await after_payout(paid)

    async with pool.connection() as conn:
        # False means another worker (or an earlier delivery of this job) recorded it first; it got the same payout from
        # the provider, so nothing is lost.
        await mark_refund_issued(conn, merchant_id, refund_id, paid.id, PAYOUT_ACTOR)
    return IssueResult(IssueOutcome.ISSUED, paid.id, paid.replayed)
