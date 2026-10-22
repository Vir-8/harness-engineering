"""reconcile: our refunds next to the provider's payouts, matched by the payout key each payout carries."""

import pytest

from refund_harness.payments.client import ProviderRefund
from refund_harness.payments.reconcile import reconcile


def ours(refund_id: int, status: str = "issued", amount: int = 5000) -> dict:
    return {"refund_id": refund_id, "status": status, "amount_cents": amount}


def paid(refund_id: int, amount: int = 5000, merchant: str = "acme", n: int = 1) -> ProviderRefund:
    return ProviderRefund(id=f"re_{refund_id}_{n}", amount_cents=amount, reference=f"{merchant}:refund-{refund_id}")


def states(lines) -> dict:
    return {line.reference: line.state for line in lines}


def test_each_kind_of_line():
    lines = reconcile(
        "acme",
        [ours(1), ours(2, "approved"), ours(3, "approved"), ours(4), ours(5), ours(6, amount=5000)],
        [paid(1), paid(3), paid(5, n=1), paid(5, n=2), paid(6, amount=4000), paid(99)],
    )

    assert states(lines) == {
        "acme:refund-1": "matched",
        "acme:refund-2": "awaiting_payout",
        "acme:refund-3": "paid_not_recorded",  # the crash gap, caught
        "acme:refund-4": "missing_at_provider",
        "acme:refund-5": "duplicate_payout",
        "acme:refund-6": "amount_mismatch",
        "acme:refund-99": "unknown_payout",
    }
    assert [line.ok for line in lines if line.refund_id in (1, 2)] == [True, True]
    assert not any(line.ok for line in lines if line.refund_id not in (1, 2))


def test_duplicate_line_lists_every_payout():
    [line] = reconcile("acme", [ours(5)], [paid(5, n=1), paid(5, n=2)])

    assert line.provider_refund_ids == ("re_5_1", "re_5_2")
    assert line.provider_amount_cents == 10000


@pytest.mark.parametrize("other", ["globex", "acme2"], ids=["another_merchant", "a_merchant_whose_name_starts_like_this_one"])
def test_other_merchants_payouts_are_left_out(other):
    lines = reconcile("acme", [ours(1)], [paid(1), paid(1, merchant=other), paid(7, merchant=other)])

    assert states(lines) == {"acme:refund-1": "matched"}
