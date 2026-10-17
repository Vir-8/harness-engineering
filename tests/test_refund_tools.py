from datetime import datetime, timedelta, timezone

from refund_harness.refund_tools import format_order

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)  # fixed clock: results never depend on today's date


def make_row(**overrides) -> dict:
    row = {"item": "headphones", "status": "delivered", "total_cents": 5900, "delivered_at": NOW - timedelta(days=3)}
    return row | overrides


def test_delivered_order_shows_item_dollars_and_relative_days():
    text = format_order("A100", make_row(), NOW)
    assert "headphones" in text
    assert "$59.00" in text
    assert "3 days ago" in text


def test_undelivered_order_says_not_delivered_yet():
    text = format_order("A300", make_row(status="shipped", delivered_at=None), NOW)
    assert "not delivered yet" in text
    assert "None" not in text


def test_cents_are_zero_padded():
    assert "$0.05" in format_order("A1", make_row(total_cents=5), NOW)


def test_one_day_is_singular():
    text = format_order("A1", make_row(delivered_at=NOW - timedelta(days=1)), NOW)
    assert "1 day ago" in text and "1 days" not in text
