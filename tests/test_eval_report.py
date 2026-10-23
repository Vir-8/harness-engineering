"""Summaries, comparisons and saving: built from hand-made runs, so every number can be checked by eye."""

from refund_harness.evals.report import (
    case_pass_rates,
    compare,
    load_report,
    p95,
    save_report,
    scorer_rates,
    summarize,
)
from refund_harness.evals.results import CaseRun, RunReport


def a_run(case_id="c1", passed=True, category="policy", repeat=1, **fields) -> CaseRun:
    scores = fields.pop("scores", {"completed": True, "mentions": passed})
    return CaseRun(case_id=case_id, category=category, merchant="acme", repeat=repeat, scores=scores, **fields)


def a_report(runs: list[CaseRun], label="baseline", model="claude-sonnet-5-5") -> RunReport:
    return RunReport(
        label=label, started_at="2026-10-19T10:00:00", git_sha="abc1234", git_dirty=False,
        agent_model=model, judge_model=None, embedding_model="gemini-embedding-2", eval_now="2026-10-18T00:00:00", runs=runs,
    )


def test_pass_rate_per_case_averages_its_repeats():
    report = a_report([a_run("c1", True, repeat=1), a_run("c1", False, repeat=2), a_run("c2", True)])

    assert case_pass_rates(report) == {"c1": 0.5, "c2": 1.0}


def test_scorer_rates_count_only_applicable_runs():
    report = a_report([
        a_run(scores={"completed": True, "refund_state": True}),
        a_run(scores={"completed": True, "refund_state": False}),
        a_run(scores={"completed": True, "refund_state": None}),  # not applicable: left out
    ])

    assert scorer_rates(report) == {"completed": (3, 3), "refund_state": (1, 2)}


def test_summary_adds_up_cost_tokens_and_latency():
    report = a_report([
        a_run(latency_s=10, cost_usd=0.04, input_tokens=6000, output_tokens=200, judge_cost_usd=0.01, attempts=2),
        a_run("c2", False, latency_s=20, cost_usd=0.06, input_tokens=8000, output_tokens=300, repeated_calls=1),
        a_run("c3", latency_s=30, cost_usd=0.05, input_tokens=1000, output_tokens=100, attempts=3),
    ])

    summary = summarize(report)

    assert summary["pass_rate"] == 2 / 3
    assert summary["latency_mean_s"] == 20
    assert summary["agent_cost_usd"] == 0.15 and summary["cost_per_run_usd"] == 0.05
    assert summary["judge_cost_usd"] == 0.01
    assert (summary["input_tokens"], summary["output_tokens"]) == (15000, 600)
    assert summary["repeated_calls"] == 1
    assert summary["retried"] == 2  # runs that needed more than one attempt
    assert summary["failing_cases"] == ["c2"]


def test_p95_is_the_value_most_runs_stay_under():
    assert p95([float(n) for n in range(1, 101)]) == 95.0
    assert p95([]) == 0.0


def test_comparison_names_regressions_and_fixes():
    before = a_report([a_run("c1", True), a_run("c2", False), a_run("c3", True), a_run("only-a")], label="old", model="model-a")
    after = a_report([a_run("c1", False), a_run("c2", True), a_run("c3", True), a_run("only-b")], label="new", model="model-b")

    text = compare(before, after)

    assert "got WORSE:  c1 (100% -> 0%)" in text
    assert "got BETTER: c2 (0% -> 100%)" in text
    assert "model-a" in text and "model-b" in text
    assert "only-a" in text and "only-b" in text  # not compared, but mentioned


def test_saved_report_loads_back_identical(tmp_path):
    report = a_report([a_run("c1", latency_s=1.5, cost_usd=0.04)], label="my-run")

    path = save_report(report, tmp_path)

    assert path.name.endswith("-my-run.json")
    assert load_report(path) == report
