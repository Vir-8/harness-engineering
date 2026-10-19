"""The judge-versus-human check: sheet building, label merging, and the agreement arithmetic."""

import yaml

from refund_harness.agent.types import ToolSpec
from refund_harness.evals.judge_check import (
    agreement,
    build_sheet,
    cohens_kappa,
    merge_labels,
    render_agreement,
    render_sheet,
)
from refund_harness.evals.results import CaseRun, JudgeVerdict, RunReport, ToolRun


def judged(case_id: str, supported: bool, declines: bool = False, repeat: int = 1) -> CaseRun:
    return CaseRun(
        case_id=case_id, category="policy", merchant="acme", question=f"Question for {case_id}?", repeat=repeat,
        answer=f"Answer for {case_id}.", tools=[ToolRun(name="search_policy", arguments={}, output="Return window\n30 days.")],
        judge=JudgeVerdict(supported=supported, declines=declines, reason=f"reason for {case_id}"),
    )


def a_report(runs: list[CaseRun]) -> RunReport:
    return RunReport(label="r", started_at="2026-10-19T10:00:00", git_sha="abc", git_dirty=False, agent_model="m",
                     judge_model="j", embedding_model="e", eval_now="2026-10-18T00:00:00", runs=runs)


def a_sheet(entries: list[dict], verdicts: dict) -> dict:
    return {"entries": entries, "judged_by": verdicts}


def label(entry_id: str, supported: bool | None, declines: bool | None = None) -> dict:
    return {"id": entry_id, "question": "q", "evidence": "e", "answer": "a", "human_supported": supported, "human_declines": declines}


# --- building the sheet ---

def test_every_run_the_judge_called_unsupported_is_on_the_sheet():
    runs = [judged(f"bad-{i}", False) for i in range(3)] + [judged(f"ok-{i}", True) for i in range(20)]

    sheet = build_sheet(a_report(runs))

    ids = {e["id"] for e in sheet["entries"]}
    assert {"bad-0#1", "bad-1#1", "bad-2#1"} <= ids


def test_supported_verdicts_are_sampled_too_so_leniency_can_be_checked():
    runs = [judged("bad", False)] + [judged(f"ok-{i}", True) for i in range(20)]

    sheet = build_sheet(a_report(runs))

    verdicts = [v["supported"] for v in sheet["judged_by"].values()]
    assert verdicts.count(True) >= 6 and verdicts.count(False) == 1


def test_runs_the_judge_never_saw_are_left_out():
    unjudged = CaseRun(case_id="x", category="policy", merchant="acme")

    assert build_sheet(a_report([unjudged, judged("y", False)]))["judged_by"].keys() == {"y#1"}


def test_the_entries_hold_no_hint_of_the_judges_verdict():
    sheet = build_sheet(a_report([judged("bad", False), judged("ok", True)] + [judged(f"o{i}", True) for i in range(8)]))

    for entry in sheet["entries"]:
        assert set(entry) == {"id", "question", "evidence", "answer", "human_supported", "human_declines"}
        assert "reason" not in str(entry)


def test_the_same_seed_gives_the_same_sheet_so_regenerating_is_stable():
    runs = [judged("bad", False)] + [judged(f"ok-{i}", True) for i in range(20)]

    assert build_sheet(a_report(runs)) == build_sheet(a_report(runs))


def test_the_judges_verdicts_are_rendered_below_a_stop_marker():
    text = render_sheet(build_sheet(a_report([judged("bad", False)] + [judged(f"o{i}", True) for i in range(8)])),
                        [ToolSpec(name="get_order", description="Look up an order.", parameters={})])

    assert text.index("STOP: the judge's verdicts are below") < text.index("judged_by:")
    assert "reason for bad" not in text[: text.index("STOP")]  # nothing about the verdict above the marker
    assert "get_order: Look up an order." in text  # the tool descriptions are on the sheet, as the judge saw them


def test_a_rendered_sheet_loads_back_as_yaml():
    sheet = build_sheet(a_report([judged("bad", False)] + [judged(f"o{i}", True) for i in range(8)]))

    loaded = yaml.safe_load(render_sheet(sheet, []))

    assert loaded["entries"] == sheet["entries"] and loaded["judged_by"] == sheet["judged_by"]


# --- keeping labels when the sheet is rebuilt ---

def test_regenerating_the_sheet_keeps_the_labels_a_person_already_wrote():
    old = a_sheet([label("a#1", True, False), label("b#1", None)], {"a#1": {"supported": True, "declines": False, "reason": "r"}})
    new = a_sheet([label("a#1", None), label("b#1", None), label("c#1", None)], {"a#1": {}, "b#1": {}, "c#1": {}})

    merged = merge_labels(new, old)

    by_id = {e["id"]: e for e in merged["entries"]}
    assert by_id["a#1"]["human_supported"] is True  # kept
    assert by_id["c#1"]["human_supported"] is None  # new entries arrive unlabelled


def test_a_labelled_entry_that_dropped_out_of_the_new_sample_is_not_lost():
    old = a_sheet([label("gone#1", False)], {"gone#1": {"supported": True, "declines": False, "reason": "r"}})
    new = a_sheet([label("a#1", None)], {"a#1": {"supported": True, "declines": False, "reason": "r"}})

    merged = merge_labels(new, old)

    assert {e["id"] for e in merged["entries"]} == {"a#1", "gone#1"}
    assert "gone#1" in merged["judged_by"]


# --- the arithmetic ---

def test_perfect_agreement_scores_one():
    assert cohens_kappa([(True, True), (False, False), (True, True), (False, False)]) == 1.0


def test_agreement_no_better_than_chance_scores_zero():
    assert cohens_kappa([(True, True), (True, False), (False, True), (False, False)]) == 0.0


def test_kappa_is_undefined_for_no_data_or_for_one_constant_answer():
    assert cohens_kappa([]) is None
    assert cohens_kappa([(True, True), (True, True)]) is None  # both always say true: nothing to measure


def test_agreement_counts_which_way_the_judge_errs():
    verdicts = {f"{n}#1": {"supported": s, "declines": False, "reason": f"why {n}"} for n, s in
                [("a", True), ("b", True), ("c", False), ("d", False)]}
    sheet = a_sheet(
        [label("a#1", True, False), label("b#1", False, False), label("c#1", True, False), label("d#1", False, False)], verdicts
    )

    result = agreement(sheet)["supported"]

    assert result["labelled"] == 4 and result["agreement"] == 0.5
    assert result["judge_true_human_false"] == 1  # b: the judge was too lenient
    assert result["judge_false_human_true"] == 1  # c: the judge was too strict
    assert {row[0] for row in result["disagreements"]} == {"b#1", "c#1"}


def test_unlabelled_entries_are_not_counted():
    verdicts = {"a#1": {"supported": True, "declines": False, "reason": "r"}, "b#1": {"supported": False, "declines": False, "reason": "r"}}
    sheet = a_sheet([label("a#1", True, False), label("b#1", None, None)], verdicts)

    assert agreement(sheet)["supported"]["labelled"] == 1


def test_the_rendered_result_names_each_disagreement_with_the_judges_reason():
    verdicts = {"a#1": {"supported": False, "declines": False, "reason": "claim not in evidence"}}

    text = render_agreement(agreement(a_sheet([label("a#1", True, False)], verdicts)))

    assert "DISAGREE a#1" in text and "claim not in evidence" in text and "too strict" in text


def test_with_no_labels_yet_it_says_so_instead_of_dividing_by_zero():
    assert "no labelled entries yet" in render_agreement(agreement(a_sheet([label("a#1", None)], {"a#1": {"supported": True, "declines": False, "reason": "r"}})))
