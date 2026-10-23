"""check_refund_eligibility: the business rules, with no database.

It returns a list of reasons the refund is NOT allowed. An empty list means it is allowed.
Every case starts from a normal order (delivered, $59.00, asked about 3 days after delivery)
and changes only the one thing it is about.
"""

from datetime import UTC, datetime, timedelta

import pytest
from helpers import A100_TOTAL_CENTS, DAY

from refund_harness.data.repository import check_refund_eligibility

DELIVERED = datetime(2026, 10, 15, tzinfo=UTC)


def rejections_for(after_delivery: timedelta = 3 * DAY, amount_cents: int = A100_TOTAL_CENTS, **order_changes) -> list[str]:
    order = {"item": "headphones", "status": "delivered", "total_cents": A100_TOTAL_CENTS, "delivered_at": DELIVERED}
    order.update(order_changes)
    return check_refund_eligibility(order, amount_cents, now=DELIVERED + after_delivery)


@pytest.mark.parametrize(
    "change",
    [
        {},
        {"amount_cents": 100},
        {"after_delivery": 30 * DAY + timedelta(hours=5)},  # the rule counts whole days: 30 days 5 hours is "30 days"
    ],
    ids=["full_refund_soon_after_delivery", "partial_refund", "30_days_and_5_hours_still_counts"],
)
def test_these_refunds_are_allowed(change):
    assert rejections_for(**change) == []


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"amount_cents": A100_TOTAL_CENTS + 1}, "Cannot refund more"),
        ({"after_delivery": 31 * DAY}, "over 30 days"),
        # An order on its way has no delivery date either; it once got "Delivery date not found" as a second reason.
        ({"status": "shipped", "delivered_at": None}, "has not been delivered"),
        ({"delivered_at": None}, "Delivery date not found"),  # delivered, but the date is missing: no crash
        ({"after_delivery": -1 * DAY}, "Invalid order date"),  # delivered in the future
    ],
    ids=["above_the_order_total", "31_days_after_delivery", "still_on_its_way", "no_delivery_date", "delivery_date_in_the_future"],
)
def test_each_problem_gives_exactly_one_reason(change, reason):
    [only_reason] = rejections_for(**change)

    assert reason in only_reason


def test_every_problem_is_reported_at_once():
    rejections = rejections_for(status="shipped", amount_cents=A100_TOTAL_CENTS + 1, after_delivery=31 * DAY)

    assert len(rejections) == 3
