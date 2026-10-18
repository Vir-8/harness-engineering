from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from helpers import A100_TOTAL_CENTS, ACME, DAY, GLOBEX, fixed_clock, moment_after_delivery
from refund_harness.refund_tools import build_registry, format_order, format_refund_result
from refund_harness.repository import RefundOutcome, SubmitRefundResult

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


# --- format_refund_result: pure. This text is all the model sees of the outcome. ---

def test_created_says_pending_approval_and_never_claims_money_moved():
    text = format_refund_result("A100", 5900, SubmitRefundResult(RefundOutcome.CREATED, refund_id=7))

    assert "pending human approval" in text
    assert "$59.00" in text and "7" in text
    assert "No money has been refunded yet" in text


def test_ineligible_lists_every_reason():
    result = SubmitRefundResult(RefundOutcome.INELIGIBLE, reasons=("too old", "over the total"))

    text = format_refund_result("A100", 5900, result)

    assert "too old" in text and "over the total" in text
    assert "(" not in text  # no leaked tuple like ('too old', ...)


def test_duplicate_says_a_request_already_exists():
    text = format_refund_result("A100", 5900, SubmitRefundResult(RefundOutcome.DUPLICATE))

    assert "already exists" in text


# --- the tools through the registry, against the real DB (the same path the agent loop takes) ---

REFUND_A100 = {"order_id": "A100", "amount_cents": A100_TOTAL_CENTS}


async def test_get_order_tool_describes_the_order(pool):
    registry = build_registry(pool, ACME)

    text = await registry.get("get_order").run({"order_id": "A100"})

    assert "headphones" in text


async def test_submit_tool_creates_a_pending_refund(pool, refunds):
    now = await moment_after_delivery(pool, 3 * DAY)
    registry = build_registry(pool, ACME, now=fixed_clock(now))

    text = await registry.get("submit_order_refund").run(REFUND_A100)

    assert "pending human approval" in text
    assert await refunds.rows() == [
        {"merchant_id": ACME, "order_id": "A100", "amount_cents": A100_TOTAL_CENTS, "status": "pending"}
    ]


async def test_submit_tool_explains_why_an_order_is_ineligible(pool, refunds):
    now = await moment_after_delivery(pool, 45 * DAY)
    registry = build_registry(pool, ACME, now=fixed_clock(now))

    text = await registry.get("submit_order_refund").run(REFUND_A100)

    assert "not eligible" in text and "30 days" in text
    assert await refunds.count() == 0


async def test_submit_tool_reports_a_repeat_request_as_a_duplicate(pool, refunds):
    now = await moment_after_delivery(pool, 3 * DAY)
    submit_tool = build_registry(pool, ACME, now=fixed_clock(now)).get("submit_order_refund")

    await submit_tool.run(REFUND_A100)
    text = await submit_tool.run(REFUND_A100)

    assert "already exists" in text
    assert await refunds.count() == 1


async def test_another_merchants_order_looks_exactly_like_a_missing_one(pool, refunds):
    globex = build_registry(pool, GLOBEX)  # A300 exists, but only for acme

    refund_text = await globex.get("submit_order_refund").run({"order_id": "A300", "amount_cents": 100})
    lookup_text = await globex.get("get_order").run({"order_id": "A300"})

    assert refund_text == lookup_text  # nothing reveals the order exists for someone else
    assert await refunds.count() == 0


async def test_the_model_cannot_choose_the_merchant(pool, refunds):
    now = await moment_after_delivery(pool, 3 * DAY)
    submit_tool = build_registry(pool, ACME, now=fixed_clock(now)).get("submit_order_refund")

    # a model trying to smuggle in another tenant: the extra field is ignored...
    await submit_tool.run({**REFUND_A100, "merchant_id": GLOBEX})

    assert [row["merchant_id"] for row in await refunds.rows()] == [ACME]
    # ...and the tool never even advertises a merchant_id parameter
    assert set(submit_tool.spec().parameters["properties"]) == {"order_id", "amount_cents"}


@pytest.mark.parametrize("bad_amount", [0, -100, "lots"], ids=["zero", "negative", "not_a_number"])
async def test_submit_tool_rejects_bad_amounts_before_touching_the_db(pool, bad_amount):
    submit_tool = build_registry(pool, ACME).get("submit_order_refund")

    with pytest.raises(ValidationError):
        await submit_tool.run({"order_id": "A100", "amount_cents": bad_amount})


def test_the_model_is_never_given_a_way_to_approve_refunds(pool):
    # Approval is a human decision. If this list ever grows, someone must decide that on purpose.
    registry = build_registry(pool, ACME)

    assert sorted(registry.names()) == ["get_order", "get_refund_policy", "submit_order_refund"]
