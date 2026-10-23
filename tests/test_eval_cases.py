"""The eval case file and its loader: the labels are checked for typos and contradictions before any run spends tokens."""

import pytest
from pydantic import ValidationError

from refund_harness.data.repository import RefundOutcome, SubmitRefundResult
from refund_harness.evals.cases import CATEGORIES, EvalCase, load_cases
from refund_harness.evals.scorers import found
from refund_harness.refund_tools import format_refund_result


def make_case(**overrides) -> dict:
    return {"id": "case-1", "merchant": "acme", "category": "policy", "question": "How long to return?"} | overrides


# --- the real case file ---

def test_case_file_covers_every_category_and_merchant():
    cases = load_cases()

    assert len(cases) >= 30
    assert {case.category for case in cases} == CATEGORIES
    assert {case.merchant for case in cases} == {"acme", "globex"}


async def test_expected_chunks_are_real_policy_titles(pool):
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT merchant_id, title FROM policy_chunks")
        titles = {(row["merchant_id"], row["title"]) for row in await cur.fetchall()}

    for case in load_cases():
        if case.expected_chunk:
            assert (case.merchant, case.expected_chunk) in titles, f"{case.id}: no such chunk for {case.merchant}"


# --- the validation that protects the labels ---

@pytest.mark.parametrize(
    "bad",
    [
        {"merchant": "initech"},
        {"category": "vibes"},
        {"tools_called": ["delete_everything"]},
        {"tools_called": ["get_order"], "tools_not_called": ["get_order"]},
        {"must_mention": [""]},
        {"must_mention": ["30 days||30-day"]},
        {"refund_amount_cents": 5900},
        {"refund_created": False, "refund_amount_cents": 5900},
        {"should_abstain": True, "refund_created": True},
        {"expected_chink": "Return window"},
    ],
    ids=["merchant", "category", "tool", "required_and_forbidden", "blank_word", "blank_alternative",
         "amount_without_refund", "amount_with_no_refund", "abstain_and_refund", "typo_in_field_name"],
)
def test_a_bad_label_is_refused(bad):
    with pytest.raises(ValidationError):
        EvalCase(**make_case(**bad))


def test_forbidden_phrases_miss_our_own_wording():
    """A word check cannot see negation. "No money has been refunded yet" is the TOOL's wording for a correct, pending
    request, and the model echoes it: a forbidden phrase that matched it would fail correct answers."""
    harness_wording = [
        format_refund_result("A100", 5900, SubmitRefundResult(RefundOutcome.CREATED, refund_id=7)),
        format_refund_result("A100", 5900, SubmitRefundResult(RefundOutcome.DUPLICATE)),
        *(
            format_refund_result("A100", 5900, SubmitRefundResult(RefundOutcome.DUPLICATE, refund_id=7, existing_status=s, existing_amount_cents=5900))
            for s in ("pending", "approved", "issued")
        ),
        format_refund_result("A200", 3500, SubmitRefundResult(RefundOutcome.INELIGIBLE, reasons=("too old",))),
    ]

    for case in load_cases():
        for pattern in case.must_not_mention:
            for text in harness_wording:
                assert not found(text, pattern), f"{case.id}: {pattern!r} matches the harness's own text: {text!r}"
