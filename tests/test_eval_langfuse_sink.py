"""The Langfuse sink, tested against a fake client that mimics what the real SDK does (including its own event loop)."""

import asyncio
from types import SimpleNamespace

from refund_harness.agent.llm import FakeLLM
from refund_harness.agent.types import LLMResponse
from refund_harness.data.db import create_pool
from refund_harness.evals.cases import EvalCase, load_cases
from refund_harness.evals.langfuse_sink import (
    DATASET_NAME,
    LiveEval,
    case_to_item,
    evaluations_for,
    run_in_langfuse,
    sync_dataset,
)
from refund_harness.evals.results import CaseRun
from refund_harness.retrieval.embedder import FakeEmbedder


def a_case(**overrides) -> EvalCase:
    return EvalCase(**({"id": "case-1", "merchant": "acme", "category": "order", "question": "What is the status of order A100?"} | overrides))


class FakeLangfuse:
    """Stands in for the SDK client. Like the real run_experiment, it is synchronous and runs the task on a loop of its own."""

    def __init__(self):
        self.datasets, self.items, self.experiments = [], {}, []

    def create_dataset(self, **kwargs):
        self.datasets.append(kwargs["name"])

    def create_dataset_item(self, **kwargs):
        self.items[kwargs["id"]] = kwargs  # same id again: updated in place, as in Langfuse

    def get_dataset(self, name):
        items = [SimpleNamespace(id=i, input=k["input"], metadata=k["metadata"], expected_output=k["expected_output"]) for i, k in self.items.items()]
        return SimpleNamespace(items=items)

    def run_experiment(self, *, name, run_name, data, task, evaluators, run_evaluators, max_concurrency, metadata):
        async def go():
            evaluations = {}
            for item in data:
                output = await task(item=item)
                evaluations[item.id] = [e for ev in evaluators for e in ev(input=item.input, output=output, expected_output=item.expected_output, metadata=item.metadata)]
            return evaluations

        evaluations = asyncio.run(go())
        run_level = [ev(item_results=[]) for ev in run_evaluators]
        self.experiments.append(dict(name=name, run_name=run_name, metadata=metadata, max_concurrency=max_concurrency, items=[i.id for i in data]))
        return SimpleNamespace(evaluations=evaluations, run_level=run_level)


# --- the dataset ---

def test_a_case_becomes_a_dataset_item_with_its_id_input_labels_and_category():
    item = case_to_item(a_case(tools_called=["get_order"], must_mention=["delivered"], refund_created=False, notes="why"))

    assert item["id"] == "case-1" and item["dataset_name"] == DATASET_NAME
    assert item["input"] == {"merchant": "acme", "question": "What is the status of order A100?"}
    assert item["expected_output"] == {"tools_called": ["get_order"], "must_mention": ["delivered"], "refund_created": False}
    assert item["metadata"] == {"case_id": "case-1", "category": "order", "notes": "why"}


def test_labels_a_case_does_not_use_are_left_out_of_the_expected_output():
    assert case_to_item(a_case())["expected_output"] == {}


def test_every_real_case_converts():
    assert len([case_to_item(case) for case in load_cases()]) >= 30


def test_syncing_creates_the_dataset_and_one_item_per_case_and_is_safe_to_repeat():
    client, cases = FakeLangfuse(), [a_case(id="case-1"), a_case(id="case-2")]

    sync_dataset(client, cases)
    sync_dataset(client, cases)

    assert set(client.items) == {"case-1", "case-2"}  # the second sync updated in place


# --- the scores ---

def test_each_applicable_check_becomes_a_score_and_not_applicable_ones_are_skipped():
    run = CaseRun(case_id="c", category="policy", merchant="acme", latency_s=12.345, cost_usd=0.05,
                  scores={"completed": True, "mentions": False, "refund_state": None})

    scores = {e.name: e.value for e in evaluations_for(run)}

    assert scores == {"completed": 1.0, "mentions": 0.0, "passed": 0.0, "latency_s": 12.35, "cost_usd": 0.05}


# --- running inside the experiment ---

async def run_through_fake_langfuse(cases, llm, client=None, **kwargs):
    client = client or FakeLangfuse()
    pools = []

    async def make_pool():
        pools.append(await create_pool())
        return pools[0]

    runs, now, result = await asyncio.to_thread(
        run_in_langfuse, client, cases, label="my-run", metadata={"agent_model": "m"}, make_llm=lambda: llm,
        make_embedder=FakeEmbedder, judge=None, retries=0, make_pool=make_pool, **kwargs,
    )
    return client, runs, now, result, pools


async def test_the_cases_run_as_one_experiment_and_come_back_as_our_own_records(refunds):
    cases = [a_case(id="case-1"), a_case(id="case-2", question="Where is order A300?")]
    llm = FakeLLM([LLMResponse(text="Delivered."), LLMResponse(text="Shipped.")])

    client, runs, now, result, _ = await run_through_fake_langfuse(cases, llm)

    assert [r.case_id for r in runs] == ["case-1", "case-2"] and [r.answer for r in runs] == ["Delivered.", "Shipped."]
    [experiment] = client.experiments
    assert experiment["run_name"] == "my-run" and experiment["max_concurrency"] == 1 and experiment["items"] == ["case-1", "case-2"]
    assert now is not None


async def test_only_the_chosen_cases_run_even_though_the_dataset_holds_more(refunds):
    client = FakeLangfuse()
    sync_dataset(client, [a_case(id="case-1"), a_case(id="case-2"), a_case(id="case-3")])

    client, runs, _, _, _ = await run_through_fake_langfuse([a_case(id="case-2")], FakeLLM([LLMResponse(text="x")]), client)

    assert client.experiments[0]["items"] == ["case-2"] and [r.case_id for r in runs] == ["case-2"]


async def test_each_item_gets_its_scores_and_the_run_gets_its_totals(refunds):
    cases = [a_case(id="case-1", must_mention=["delivered"]), a_case(id="case-2", must_mention=["shipped"])]
    llm = FakeLLM([LLMResponse(text="It was delivered."), LLMResponse(text="Nothing to say.")])

    _, _, _, result, _ = await run_through_fake_langfuse(cases, llm)

    assert {e.name: e.value for e in result.evaluations["case-1"]}["passed"] == 1.0
    assert {e.name: e.value for e in result.evaluations["case-2"]}["passed"] == 0.0
    assert {e.name: e.value for e in result.run_level}["pass_rate"] == 0.5


async def test_the_database_pool_is_made_in_the_experiments_loop_and_closed_after_the_last_case(refunds):
    _, _, _, _, pools = await run_through_fake_langfuse([a_case(id="case-1"), a_case(id="case-2")], FakeLLM([LLMResponse(text="a"), LLMResponse(text="b")]))

    assert len(pools) == 1  # created once, lazily
    assert pools[0].closed  # and closed once the last case finished


async def test_progress_is_reported_per_case(refunds):
    seen = []

    await run_through_fake_langfuse([a_case(id="case-1")], FakeLLM([LLMResponse(text="a")]), on_result=seen.append)

    assert [r.case_id for r in seen] == ["case-1"]


def test_an_evaluator_for_an_unknown_case_has_nothing_to_say():
    live = LiveEval([a_case()], make_llm=lambda: None, make_embedder=FakeEmbedder, judge=None, retries=0)

    assert live.evaluator(metadata={"case_id": "never-ran"}) == []
    assert live.evaluator(metadata=None) == []
