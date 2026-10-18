"""check_refund_eligibility: the business rules, with no database.

It returns a list of reasons the refund is NOT allowed. An empty list means it is allowed.
Every test starts from a normal order (delivered, $59.00, asked about 3 days after delivery)
and changes only the one thing it is about.
"""

from datetime import datetime, timedelta, timezone

from helpers import A100_TOTAL_CENTS, DAY

from refund_harness.repository import check_refund_eligibility

DELIVERED = datetime(2026, 10, 15, tzinfo=timezone.utc)


def rejections_for(after_delivery: timedelta = 3 * DAY, amount_cents: int = A100_TOTAL_CENTS, **order_changes) -> list[str]:
    order = {"item": "headphones", "status": "delivered", "total_cents": A100_TOTAL_CENTS, "delivered_at": DELIVERED}
    order.update(order_changes)
    return check_refund_eligibility(order, amount_cents, now=DELIVERED + after_delivery)


def mentions(rejections: list[str], fragment: str) -> bool:
    return any(fragment in reason for reason in rejections)


# --- allowed ---

def test_full_refund_shortly_after_delivery_is_allowed():
    assert rejections_for() == []


def test_partial_refund_is_allowed():
    assert rejections_for(amount_cents=100) == []


def test_30_days_and_5_hours_after_delivery_still_counts_as_within_30_days():
    # the rule counts whole days: 30 days 5 hours is "30 days"
    assert rejections_for(after_delivery=30 * DAY + timedelta(hours=5)) == []


# --- rejected, one reason each ---

def test_refund_above_the_order_total_is_rejected():
    assert mentions(rejections_for(amount_cents=A100_TOTAL_CENTS + 1), "Cannot refund more")


def test_31_days_after_delivery_is_too_late():
    assert mentions(rejections_for(after_delivery=31 * DAY), "over 30 days")


def test_order_that_has_not_been_delivered_is_rejected():
    assert mentions(rejections_for(status="shipped", delivered_at=None), "has not been delivered")


def test_delivered_order_with_no_delivery_date_is_rejected_without_crashing():
    assert mentions(rejections_for(delivered_at=None), "Order date not found")


def test_delivery_date_in_the_future_is_rejected():
    assert mentions(rejections_for(after_delivery=-1 * DAY), "Invalid order date")


# --- several problems ---

def test_every_problem_is_reported_at_once():
    rejections = rejections_for(status="shipped", amount_cents=A100_TOTAL_CENTS + 1, after_delivery=31 * DAY)

    assert len(rejections) == 3
