from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from helpers import A100_TOTAL_CENTS, ACME, DAY, GLOBEX, SANDBOX, fixed_clock, moment_after_delivery
from refund_harness.embedder import FakeEmbedder
from refund_harness.refund_tools import build_registry, format_order, format_policy_results, format_refund_result
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
    registry = build_registry(pool, ACME, FakeEmbedder())

    text = await registry.get("get_order").run({"order_id": "A100"})

    assert "headphones" in text


async def test_submit_tool_creates_a_pending_refund(pool, refunds):
    now = await moment_after_delivery(pool, 3 * DAY)
    registry = build_registry(pool, ACME, FakeEmbedder(), now=fixed_clock(now))

    text = await registry.get("submit_order_refund").run(REFUND_A100)

    assert "pending human approval" in text
    assert await refunds.rows() == [
        {"merchant_id": ACME, "order_id": "A100", "amount_cents": A100_TOTAL_CENTS, "status": "pending"}
    ]


async def test_submit_tool_explains_why_an_order_is_ineligible(pool, refunds):
    now = await moment_after_delivery(pool, 45 * DAY)
    registry = build_registry(pool, ACME, FakeEmbedder(), now=fixed_clock(now))

    text = await registry.get("submit_order_refund").run(REFUND_A100)

    assert "not eligible" in text and "30 days" in text
    assert await refunds.count() == 0


async def test_submit_tool_reports_a_repeat_request_as_a_duplicate(pool, refunds):
    now = await moment_after_delivery(pool, 3 * DAY)
    submit_tool = build_registry(pool, ACME, FakeEmbedder(), now=fixed_clock(now)).get("submit_order_refund")

    await submit_tool.run(REFUND_A100)
    text = await submit_tool.run(REFUND_A100)

    assert "already exists" in text
    assert await refunds.count() == 1


async def test_another_merchants_order_looks_exactly_like_a_missing_one(pool, refunds):
    globex = build_registry(pool, GLOBEX, FakeEmbedder())  # A300 exists, but only for acme

    refund_text = await globex.get("submit_order_refund").run({"order_id": "A300", "amount_cents": 100})
    lookup_text = await globex.get("get_order").run({"order_id": "A300"})

    assert refund_text == lookup_text  # nothing reveals the order exists for someone else
    assert await refunds.count() == 0


async def test_the_model_cannot_choose_the_merchant(pool, refunds):
    now = await moment_after_delivery(pool, 3 * DAY)
    submit_tool = build_registry(pool, ACME, FakeEmbedder(), now=fixed_clock(now)).get("submit_order_refund")

    # a model trying to smuggle in another tenant: the extra field is ignored...
    await submit_tool.run({**REFUND_A100, "merchant_id": GLOBEX})

    assert [row["merchant_id"] for row in await refunds.rows()] == [ACME]
    # ...and the tool never even advertises a merchant_id parameter
    assert set(submit_tool.spec().parameters["properties"]) == {"order_id", "amount_cents"}


@pytest.mark.parametrize("bad_amount", [0, -100, "lots"], ids=["zero", "negative", "not_a_number"])
async def test_submit_tool_rejects_bad_amounts_before_touching_the_db(pool, bad_amount):
    submit_tool = build_registry(pool, ACME, FakeEmbedder()).get("submit_order_refund")

    with pytest.raises(ValidationError):
        await submit_tool.run({"order_id": "A100", "amount_cents": bad_amount})


# --- search_policy: what the model reads after a policy search ---

def chunk(title: str, body: str, distance: float) -> dict:
    return {"title": title, "body": body, "distance": distance}


def test_policy_results_show_each_relevant_chunk_in_order():
    text = format_policy_results([chunk("Return window", "30 days.", 0.2), chunk("Shipping", "Customer pays.", 0.4)])

    assert text == "Return window\n30 days.\n\nShipping\nCustomer pays."


def test_chunks_too_far_from_the_question_are_left_out():
    text = format_policy_results([chunk("Return window", "30 days.", 0.2), chunk("Gift cards", "Final sale.", 0.9)])

    assert "Return window" in text
    assert "Gift cards" not in text


def test_when_nothing_is_close_the_model_is_told_not_to_guess():
    text = format_policy_results([chunk("Gift cards", "Final sale.", 0.9)])

    assert "No policy text clearly matches" in text
    assert "Do not guess" in text


def test_no_chunks_at_all_gets_the_same_do_not_guess_message():
    assert "Do not guess" in format_policy_results([])


# --- the search_policy tool through the registry. The sandbox merchant's chunks carry FakeEmbedder vectors,
# which compare by shared words, so the questions below reuse the chunks' own words. ---

async def embedded_sandbox_tool(pool, sandbox):
    from refund_harness.ingest import embed_missing_chunks

    embedder = FakeEmbedder()
    await embed_missing_chunks(pool, embedder, merchant_id=sandbox)
    return build_registry(pool, sandbox, embedder).get("search_policy"), embedder


async def test_search_policy_returns_the_matching_chunk_text(pool, sandbox):
    search, _ = await embedded_sandbox_tool(pool, sandbox)

    text = await search.run({"question": "gift cards final sale"})

    assert "Gift cards are final sale." in text
    assert "Items may be returned" not in text  # not close enough to the question


async def test_search_policy_embeds_the_question_the_model_asked(pool, sandbox):
    search, embedder = await embedded_sandbox_tool(pool, sandbox)
    calls_before = len(embedder.calls)

    await search.run({"question": "gift cards final sale"})

    assert embedder.calls[calls_before:] == ["gift cards final sale"]


async def test_search_policy_says_so_when_nothing_matches(pool, sandbox):
    search, _ = await embedded_sandbox_tool(pool, sandbox)

    text = await search.run({"question": "weather forecast tomorrow"})

    assert "No policy text clearly matches" in text


async def test_search_policy_never_shows_another_merchants_policy(pool, sandbox):
    await embedded_sandbox_tool(pool, sandbox)
    acme_search = build_registry(pool, ACME, FakeEmbedder()).get("search_policy")

    text = await acme_search.run({"question": "zebra unicorn rainbow"})  # the sandbox's own words

    assert "zebra" not in text


@pytest.mark.parametrize("bad_question", ["", "x", "q" * 501], ids=["empty", "one_character", "too_long"])
async def test_search_policy_rejects_unusable_questions(pool, bad_question):
    search = build_registry(pool, ACME, FakeEmbedder()).get("search_policy")

    with pytest.raises(ValidationError):
        await search.run({"question": bad_question})


def test_the_model_is_never_given_a_way_to_approve_refunds(pool):
    # Approval is a human decision. If this list ever grows, someone must decide that on purpose.
    registry = build_registry(pool, ACME, FakeEmbedder())

    assert sorted(registry.names()) == ["get_order", "search_policy", "submit_order_refund"]
