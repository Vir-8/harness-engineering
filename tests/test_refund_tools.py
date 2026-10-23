"""The model's three tools (get_order, search_policy, submit_order_refund) and the text it reads back from them."""

from datetime import UTC, datetime, timedelta

import pytest
from helpers import (
    A100_TOTAL_CENTS,
    ACME,
    DAY,
    GLOBEX,
    fixed_clock,
    moment_after_delivery,
)
from pydantic import ValidationError

from refund_harness.data.repository import RefundOutcome, SubmitRefundResult
from refund_harness.refund_tools import (
    build_registry,
    format_order,
    format_policy_results,
    format_refund_result,
)
from refund_harness.retrieval.embedder import FakeEmbedder

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)  # fixed clock: results never depend on today's date


def make_row(**overrides) -> dict:
    row = {"item": "headphones", "status": "delivered", "total_cents": 5900, "delivered_at": NOW - timedelta(days=3)}
    return row | overrides


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, ["headphones", "$59.00", "3 days ago"]),
        ({"status": "shipped", "delivered_at": None}, ["not delivered yet"]),
        ({"total_cents": 5}, ["$0.05"]),
    ],
    ids=["delivered", "not_delivered_yet", "cents_zero_padded"],
)
def test_order_text_shows_what_the_model_needs(changes, expected):
    text = format_order("A100", make_row(**changes), NOW)

    for fragment in expected:
        assert fragment in text
    assert "None" not in text  # a missing delivery date must not leak into the text as Python's None


# --- format_refund_result: pure. This text is all the model sees of the outcome. ---

@pytest.mark.parametrize(
    ("status", "says"),
    [("pending", "waiting for a human to review"), ("approved", "payout is in progress"), ("issued", "paid out in full ($59.00)")],
)
def test_duplicate_says_where_the_request_stands(status, says):
    # Told only "it already exists", the model once told a customer whose refund had been PAID that it was still under review.
    result = SubmitRefundResult(RefundOutcome.DUPLICATE, refund_id=7, existing_status=status, existing_amount_cents=5900)

    text = format_refund_result("A100", 5900, result)

    assert says in text and "(7)" in text


# --- the tools through the registry, against the real DB (the same path the agent loop takes) ---

REFUND_A100 = {"order_id": "A100", "amount_cents": A100_TOTAL_CENTS}


async def test_submit_tool_creates_a_pending_refund(pool, refunds):
    now = await moment_after_delivery(pool, 3 * DAY)
    registry = build_registry(pool, ACME, FakeEmbedder(), now=fixed_clock(now))

    text = await registry.get("submit_order_refund").run(REFUND_A100)

    assert "pending human approval" in text
    assert "No money has been refunded yet" in text  # the model repeats this to the customer: it must never claim a payout
    assert await refunds.rows() == [
        {"merchant_id": ACME, "order_id": "A100", "amount_cents": A100_TOTAL_CENTS, "status": "pending"}
    ]


async def test_submit_tool_explains_ineligibility(pool, refunds):
    now = await moment_after_delivery(pool, 45 * DAY)
    registry = build_registry(pool, ACME, FakeEmbedder(), now=fixed_clock(now))

    text = await registry.get("submit_order_refund").run(REFUND_A100)

    assert "not eligible" in text and "30 days" in text
    assert await refunds.count() == 0


async def test_other_merchants_order_looks_missing(pool, refunds):
    globex = build_registry(pool, GLOBEX, FakeEmbedder())  # A300 exists, but only for acme

    refund_text = await globex.get("submit_order_refund").run({"order_id": "A300", "amount_cents": 100})
    lookup_text = await globex.get("get_order").run({"order_id": "A300"})

    assert refund_text == lookup_text  # nothing reveals the order exists for someone else
    assert await refunds.count() == 0


async def test_model_cannot_choose_the_merchant(pool, refunds):
    now = await moment_after_delivery(pool, 3 * DAY)
    submit_tool = build_registry(pool, ACME, FakeEmbedder(), now=fixed_clock(now)).get("submit_order_refund")

    # a model trying to smuggle in another tenant: the extra field is ignored...
    await submit_tool.run({**REFUND_A100, "merchant_id": GLOBEX})

    assert [row["merchant_id"] for row in await refunds.rows()] == [ACME]
    # ...and the tool never even advertises a merchant_id parameter
    assert set(submit_tool.spec().parameters["properties"]) == {"order_id", "amount_cents"}


@pytest.mark.parametrize("bad_amount", [0, -100, "lots"], ids=["zero", "negative", "not_a_number"])
async def test_bad_amounts_never_reach_the_database(pool, bad_amount):
    submit_tool = build_registry(pool, ACME, FakeEmbedder()).get("submit_order_refund")

    with pytest.raises(ValidationError):
        await submit_tool.run({"order_id": "A100", "amount_cents": bad_amount})


# --- search_policy: what the model reads after a policy search ---

def chunk(title: str, body: str, distance: float) -> dict:
    return {"title": title, "body": body, "distance": distance}


def test_distant_policy_chunks_are_dropped():
    text = format_policy_results([chunk("Return window", "30 days.", 0.2), chunk("Gift cards", "Final sale.", 0.9)])

    assert "Return window" in text
    assert "Gift cards" not in text


@pytest.mark.parametrize(
    "chunks", [[chunk("Gift cards", "Final sale.", 0.9)], []], ids=["nothing_close_enough", "no_chunks_at_all"]
)
def test_model_is_told_not_to_guess(chunks):
    text = format_policy_results(chunks)

    assert "No policy text clearly matches" in text
    assert "Do not guess" in text


# --- the search_policy tool through the registry. The sandbox merchant's chunks carry FakeEmbedder vectors,
# which compare by shared words, so the questions below reuse the chunks' own words. ---

async def embedded_sandbox_tool(pool, sandbox):
    from refund_harness.retrieval.ingest import embed_missing_chunks

    embedder = FakeEmbedder()
    await embed_missing_chunks(pool, embedder, merchant_id=sandbox)
    return build_registry(pool, sandbox, embedder).get("search_policy")


async def test_search_returns_the_matching_policy(pool, sandbox):
    search = await embedded_sandbox_tool(pool, sandbox)

    text = await search.run({"question": "gift cards final sale"})

    assert "Gift cards are final sale." in text
    assert "Items may be returned" not in text  # not close enough to the question


async def test_search_never_shows_other_merchants_policy(pool, sandbox):
    await embedded_sandbox_tool(pool, sandbox)
    acme_search = build_registry(pool, ACME, FakeEmbedder()).get("search_policy")

    text = await acme_search.run({"question": "zebra unicorn rainbow"})  # the sandbox's own words

    assert "zebra" not in text


def test_model_has_no_tool_to_approve_or_pay(pool):
    # Approval is a human decision. If this list ever grows, someone must decide that on purpose.
    registry = build_registry(pool, ACME, FakeEmbedder())

    assert sorted(registry.names()) == ["get_order", "search_policy", "submit_order_refund"]
