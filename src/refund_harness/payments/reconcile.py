"""Reconciliation: our records next to the payment provider's ledger, so a missing or doubled payout cannot hide.

Every payout carries its refund's key as its reference (payout.payout_key), so the two sides line up by that key:

    matched              issued here, paid exactly once there, same amount
    awaiting_payout      approved here, nothing paid there yet: the payout job is on its way
    paid_not_recorded    paid there, still 'approved' here: a worker died between paying and recording. The next
                         delivery of the job records it (the idempotency key replays the payout), so this heals itself.
    missing_at_provider  'issued' here but no payout there: money we believe went out never did
    duplicate_payout     more than one payout for one refund: the customer was paid twice
    amount_mismatch      paid once, but not the amount we approved
    unknown_payout       a payout under this merchant's references that matches no refund of ours
"""

from collections import defaultdict
from dataclasses import dataclass
from typing import Literal

from refund_harness.payments.client import ProviderRefund
from refund_harness.payments.payout import payout_key, payout_key_prefix

LineState = Literal[
    "matched",
    "awaiting_payout",
    "paid_not_recorded",
    "missing_at_provider",
    "duplicate_payout",
    "amount_mismatch",
    "unknown_payout",
]


@dataclass(frozen=True)
class LedgerLine:
    reference: str
    state: LineState
    refund_id: int | None
    status: str | None  # ours
    amount_cents: int | None  # ours
    provider_refund_ids: tuple[str, ...]
    provider_amount_cents: int  # the sum of the provider's payouts under this reference

    @property
    def ok(self) -> bool:
        return self.state in ("matched", "awaiting_payout")


def reconcile(merchant_id: str, ours: list[dict], theirs: list[ProviderRefund]) -> list[LedgerLine]:
    """`ours`: this merchant's approved and issued refunds (refund_id, status, amount_cents). `theirs`: the whole ledger."""
    prefix = payout_key_prefix(merchant_id)
    payouts: dict[str, list[ProviderRefund]] = defaultdict(list)
    for payout in theirs:
        if payout.reference.startswith(prefix):  # other merchants' payouts are none of this merchant's business
            payouts[payout.reference].append(payout)

    lines = []
    for refund in ours:
        reference = payout_key(merchant_id, refund["refund_id"])
        paid = payouts.pop(reference, [])
        lines.append(LedgerLine(
            reference=reference,
            state=_state(refund, paid),
            refund_id=refund["refund_id"],
            status=refund["status"],
            amount_cents=refund["amount_cents"],
            provider_refund_ids=tuple(p.id for p in paid),
            provider_amount_cents=sum(p.amount_cents for p in paid),
        ))
    for reference, paid in payouts.items():  # what is left matches nothing of ours
        lines.append(LedgerLine(
            reference=reference,
            state="unknown_payout",
            refund_id=None,
            status=None,
            amount_cents=None,
            provider_refund_ids=tuple(p.id for p in paid),
            provider_amount_cents=sum(p.amount_cents for p in paid),
        ))
    return lines


def _state(refund: dict, paid: list[ProviderRefund]) -> LineState:
    if len(paid) > 1:
        return "duplicate_payout"
    if refund["status"] == "approved":
        return "paid_not_recorded" if paid else "awaiting_payout"
    if not paid:
        return "missing_at_provider"
    return "matched" if paid[0].amount_cents == refund["amount_cents"] else "amount_mismatch"
