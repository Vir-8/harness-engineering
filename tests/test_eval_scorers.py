"""score_run: the code scorers that turn a recorded run into pass/fail. Each test builds a run by hand."""

import pytest

from refund_harness.evals.cases import EvalCase
from refund_harness.evals.results import CaseRun, JudgeVerdict, Retrieval, ToolRun
from refund_harness.evals.scorers import found, score_run


def a_case(**overrides) -> EvalCase:
    return EvalCase(**({"id": "case-1", "merchant": "acme", "category": "policy", "question": "How long to return?"} | overrides))


def a_run(**overrides) -> CaseRun:
    return CaseRun(**({"case_id": "case-1", "category": "policy", "merchant": "acme", "stop_reason": "final", "answer": "Within 30 days."} | overrides))


def tool(name: str, output: str = "ok", **arguments) -> ToolRun:
    return ToolRun(name=name, arguments=arguments, output=output)


# --- matching words ---

@pytest.mark.parametrize(
    "answer, pattern, expected",
    [
        ("You have 30 days to return it.", "30 days", True),
        ("You have THIRTY days.", "30 days|thirty", True),  # alternatives, any case
        ("That isn’t possible.", "isn't", True),  # curly apostrophes are straightened
        ("Returns are not accepted.", "30 days", False),
    ],
)
def test_matching_ignores_case_and_curly_quotes(answer, pattern, expected):
    assert found(answer, pattern) is expected


# --- one scorer at a time ---

def test_only_a_finished_run_is_completed():
    assert score_run(a_case(), a_run())["completed"] is True
    assert score_run(a_case(), a_run(error="ModelCallError: down", stop_reason=None))["completed"] is False
    assert score_run(a_case(), a_run(stop_reason="max_steps"))["completed"] is False


def test_required_and_forbidden_tools():
    case = a_case(tools_called=["get_order", "search_policy"], tools_not_called=["submit_order_refund"])

    assert score_run(case, a_run(tools=[tool("get_order"), tool("search_policy")]))["tools_called"] is True
    assert score_run(case, a_run(tools=[tool("get_order")]))["tools_called"] is False
    assert score_run(case, a_run(tools=[tool("get_order")]))["tools_not_called"] is True
    assert score_run(case, a_run(tools=[tool("submit_order_refund")]))["tools_not_called"] is False


def test_agent_found_the_expected_chunk():
    case = a_case(expected_chunk="Return window")

    found_it = a_run(tools=[tool("search_policy", "Return window\nItems may be returned within 30 days.")])
    missed = a_run(tools=[tool("search_policy", "Gift cards\nFinal sale.")])
    never_searched = a_run(tools=[tool("get_order", "Return window")])  # the title in ANOTHER tool's output does not count

    assert score_run(case, found_it)["agent_found_chunk"] is True
    assert score_run(case, missed)["agent_found_chunk"] is False
    assert score_run(case, never_searched)["agent_found_chunk"] is False


def test_retrieval_hit_at_1_and_hit_at_3():
    case = a_case(expected_chunk="Return window")

    first = a_run(retrieval=Retrieval(rank=1, distance=0.3, best_distance=0.3, top_titles=["Return window"]))
    third = a_run(retrieval=Retrieval(rank=3, distance=0.4, best_distance=0.3, top_titles=["A", "B", "Return window"]))
    absent = a_run(retrieval=Retrieval(rank=None, distance=None, best_distance=0.3, top_titles=["A", "B", "C"]))

    assert (score_run(case, first)["retrieval_hit_at_1"], score_run(case, first)["retrieval_hit_at_3"]) == (True, True)
    assert (score_run(case, third)["retrieval_hit_at_1"], score_run(case, third)["retrieval_hit_at_3"]) == (False, True)
    assert (score_run(case, absent)["retrieval_hit_at_1"], score_run(case, absent)["retrieval_hit_at_3"]) == (False, False)


def test_required_and_forbidden_words():
    case = a_case(must_mention=["30 days|30-day", "refund"], must_not_mention=["has been refunded|been issued"])

    assert score_run(case, a_run(answer="A full refund within 30 days."))["mentions"] is True
    assert score_run(case, a_run(answer="Within 30 days."))["mentions"] is False
    assert score_run(case, a_run(answer="Your request is pending approval."))["no_forbidden_words"] is True
    assert score_run(case, a_run(answer="Your refund has been refunded."))["no_forbidden_words"] is False


@pytest.mark.parametrize(
    "case_fields, refunds, expected",
    [
        ({"refund_created": True}, [5900], True),
        ({"refund_created": True}, [], False),
        ({"refund_created": False}, [], True),
        ({"refund_created": False}, [5900], False),
        ({"refund_created": True, "refund_amount_cents": 5900}, [5900], True),
        ({"refund_created": True, "refund_amount_cents": 5900}, [2000], False),
        ({"refund_created": True, "refund_amount_cents": 5900}, [5900, 5900], False),  # a second request is also wrong
    ],
)
def test_refund_state_is_checked_in_the_database(case_fields, refunds, expected):
    assert score_run(a_case(**case_fields), a_run(refunds_cents=refunds))["refund_state"] is expected


# --- the judge's verdicts ---

def test_abstention_needs_a_clean_decline():
    case = a_case(should_abstain=True)

    clean_decline = a_run(judge=JudgeVerdict(supported=True, declines=True, reason=""))
    decline_with_a_guess = a_run(judge=JudgeVerdict(supported=False, declines=True, reason=""))
    answered_anyway = a_run(judge=JudgeVerdict(supported=False, declines=False, reason=""))

    assert score_run(case, clean_decline)["abstains_correctly"] is True
    assert score_run(case, decline_with_a_guess)["abstains_correctly"] is False
    assert score_run(case, answered_anyway)["abstains_correctly"] is False
    assert score_run(a_case(), clean_decline)["abstains_correctly"] is None  # only cases that should abstain are asked


# --- putting scores together ---

def test_unasked_checks_are_not_applicable():
    scores = score_run(a_case(), a_run())

    assert scores["tools_called"] is None and scores["refund_state"] is None and scores["retrieval_hit_at_1"] is None
    assert scores["evidence_supported"] is None  # no judge, no verdict
    assert a_run(scores={"completed": True, "mentions": None}).passed is True
    assert a_run(scores={"completed": True, "mentions": False}).passed is False


# --- a run that failed has nothing to score ---

def test_errored_run_is_scored_on_completion_only():
    case = a_case(tools_called=["search_policy"], must_mention=["30 days"], refund_created=False)

    scores = score_run(case, a_run(error="ModelCallError: claude exited 1", stop_reason=None, answer=""))

    assert scores["completed"] is False
    assert [name for name, value in scores.items() if name != "completed" and value is not None] == []
    assert a_run(error="x", scores=scores).passed is False
