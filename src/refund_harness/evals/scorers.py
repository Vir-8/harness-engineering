"""Turning a recorded run into pass/fail scores. Pure code: no model, no database, so it is fast and exact.

Each scorer returns True (passed), False (failed) or None (this case does not ask for that check).
"""

from refund_harness.evals.cases import EvalCase
from refund_harness.evals.results import CaseRun

SCORER_NAMES = (
    "tools_called", "tools_not_called", "agent_found_chunk", "retrieval_hit_at_1", "retrieval_hit_at_3",
    "mentions", "no_forbidden_words", "refund_state", "evidence_supported", "abstains_correctly",
)


def _plain(text: str) -> str:
    """Lowercase and straighten curly quotes, so "can’t" matches the label "can't"."""
    return text.lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')


def found(answer: str, pattern: str) -> bool:
    """True if any of the alternatives in `pattern` ("a|b") appears in the answer."""
    text = _plain(answer)
    return any(_plain(alternative.strip()) in text for alternative in pattern.split("|"))


def missing_words(answer: str, patterns: list[str]) -> list[str]:
    return [pattern for pattern in patterns if not found(answer, pattern)]


def forbidden_words_found(answer: str, patterns: list[str]) -> list[str]:
    return [pattern for pattern in patterns if found(answer, pattern)]


def score_run(case: EvalCase, run: CaseRun) -> dict[str, bool | None]:
    tools_used = {tool.name for tool in run.tools}
    scores: dict[str, bool | None] = {"completed": run.error is None and run.stop_reason == "final"}
    if run.error is not None:
        # The run produced nothing to judge. Scoring the other checks would count "no refund was created" as a pass
        # and "the answer is empty" as a failure, and either would blur the per-check numbers.
        return scores | {name: None for name in SCORER_NAMES}

    scores["tools_called"] = set(case.tools_called) <= tools_used if case.tools_called else None
    scores["tools_not_called"] = not (set(case.tools_not_called) & tools_used) if case.tools_not_called else None

    if case.expected_chunk:
        scores["agent_found_chunk"] = any(
            case.expected_chunk in tool.output for tool in run.tools if tool.name == "search_policy"
        )
        scores["retrieval_hit_at_1"] = run.retrieval is not None and run.retrieval.rank == 1
        scores["retrieval_hit_at_3"] = run.retrieval is not None and run.retrieval.rank is not None
    else:
        scores["agent_found_chunk"] = scores["retrieval_hit_at_1"] = scores["retrieval_hit_at_3"] = None

    scores["mentions"] = not missing_words(run.answer, case.must_mention) if case.must_mention else None
    scores["no_forbidden_words"] = (
        not forbidden_words_found(run.answer, case.must_not_mention) if case.must_not_mention else None
    )

    if case.refund_created is None:
        scores["refund_state"] = None
    else:
        created = bool(run.refunds_cents)
        amount_ok = case.refund_amount_cents is None or run.refunds_cents == [case.refund_amount_cents]
        scores["refund_state"] = created == case.refund_created and (not case.refund_created or amount_ok)

    # judged scores: None when no judge ran, so a run without a judge is not penalised for it
    scores["evidence_supported"] = run.judge.supported if run.judge else None
    scores["abstains_correctly"] = (run.judge.declines and run.judge.supported) if run.judge and case.should_abstain else None
    return scores
