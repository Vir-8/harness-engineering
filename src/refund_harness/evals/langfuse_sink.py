"""Runs the eval cases inside Langfuse's experiment runner, so each run becomes a dataset run you can compare in its UI.

What Langfuse adds on top of the plain runner: the dataset lives in the project (synced from evals/cases.yaml), every
case's trace is stored (our own spans arrive as an AGENT with GENERATION and TOOL children, tokens and cost included,
because they follow the OpenTelemetry GenAI conventions), and runs of different models or prompts compare side by side.

One engineering catch: `run_experiment` is synchronous and runs its tasks on an event loop of its own. Our async
database pool belongs to whichever loop created it, so the pool is created INSIDE that loop, lazily, and closed after
the last case. Call run_in_langfuse from plain synchronous code, not from inside a running loop.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from langfuse import Evaluation
from psycopg_pool import AsyncConnectionPool

from refund_harness.agent.llm import LLMClient
from refund_harness.data.db import create_pool
from refund_harness.evals.cases import EvalCase
from refund_harness.evals.judge import Judge
from refund_harness.evals.results import CaseRun
from refund_harness.evals.runner import eval_now, run_case
from refund_harness.retrieval.embedder import Embedder

DATASET_NAME = "refund-agent-evals"


def case_to_item(case: EvalCase) -> dict[str, Any]:
    """The keyword arguments for create_dataset_item. The id is the case id, so syncing again updates in place."""
    labels = case.model_dump(exclude={"id", "merchant", "category", "question", "notes"}, exclude_defaults=True)
    return {
        "dataset_name": DATASET_NAME,
        "id": case.id,
        "input": {"merchant": case.merchant, "question": case.question},
        "expected_output": labels,
        "metadata": {"case_id": case.id, "category": case.category, **({"notes": case.notes} if case.notes else {})},
    }


def sync_dataset(client: Any, cases: list[EvalCase]) -> None:
    client.create_dataset(name=DATASET_NAME, description="Hand-labelled cases for the refund agent (evals/cases.yaml)")
    for case in cases:
        client.create_dataset_item(**case_to_item(case))


def evaluations_for(run: CaseRun) -> list[Evaluation]:
    """One score per check that applied to the case, plus the overall verdict, latency and cost."""
    scores = [Evaluation(name=name, value=1.0 if value else 0.0) for name, value in run.scores.items() if value is not None]
    scores.append(Evaluation(name="passed", value=1.0 if run.passed else 0.0))
    scores.append(Evaluation(name="latency_s", value=round(run.latency_s, 2)))
    scores.append(Evaluation(name="cost_usd", value=run.cost_usd))
    return scores


class LiveEval:
    """Everything one experiment needs, built lazily inside the experiment's own event loop."""

    def __init__(
        self,
        cases: list[EvalCase],
        *,
        make_llm: Callable[[], LLMClient],
        make_embedder: Callable[[], Embedder],
        judge: Judge | None,
        retries: int,
        make_pool: Callable[[], Awaitable[AsyncConnectionPool]] = create_pool,
        on_result: Callable[[CaseRun], None] | None = None,
    ):
        self.cases = {case.id: case for case in cases}
        self.results: dict[str, CaseRun] = {}
        self.now: datetime | None = None
        self._make_llm, self._make_embedder, self._judge, self._retries = make_llm, make_embedder, judge, retries
        self._make_pool, self._on_result = make_pool, on_result
        self._pool: AsyncConnectionPool | None = None
        self._lock: asyncio.Lock | None = None
        self._llm = self._embedder = None

    async def _ready(self) -> AsyncConnectionPool:
        self._lock = self._lock or asyncio.Lock()
        async with self._lock:
            if self._pool is None:
                self._pool = await self._make_pool()
                self.now = await eval_now(self._pool)
                self._llm, self._embedder = self._make_llm(), self._make_embedder()
        return self._pool

    async def task(self, *, item: Any, **kwargs: Any) -> str:
        pool = await self._ready()
        case = self.cases[item.metadata["case_id"]]
        run = await run_case(
            case, pool=pool, llm=self._llm, embedder=self._embedder, now=self.now, judge=self._judge, retries=self._retries
        )
        self.results[case.id] = run
        if self._on_result:
            self._on_result(run)
        if len(self.results) == len(self.cases):  # the last case: nothing else will use the pool
            await pool.close()
        return run.answer or f"(no answer: {run.error})"

    def evaluator(self, *, metadata: dict, **kwargs: Any) -> list[Evaluation]:
        run = self.results.get((metadata or {}).get("case_id", ""))
        return evaluations_for(run) if run else []

    def run_evaluators(self) -> list[Callable[..., Evaluation]]:
        runs = lambda: list(self.results.values())  # noqa: E731
        return [
            lambda **kw: Evaluation(name="pass_rate", value=sum(r.passed for r in runs()) / max(len(runs()), 1)),
            lambda **kw: Evaluation(name="mean_latency_s", value=round(sum(r.latency_s for r in runs()) / max(len(runs()), 1), 2)),
            lambda **kw: Evaluation(name="total_cost_usd", value=round(sum(r.cost_usd for r in runs()), 4)),
        ]


def run_in_langfuse(
    client: Any,
    cases: list[EvalCase],
    *,
    label: str,
    metadata: dict[str, str],
    make_llm: Callable[[], LLMClient],
    make_embedder: Callable[[], Embedder],
    judge: Judge | None,
    retries: int,
    make_pool: Callable[[], Awaitable[AsyncConnectionPool]] = create_pool,
    on_result: Callable[[CaseRun], None] | None = None,
) -> tuple[list[CaseRun], datetime, Any]:
    """Sync the dataset, run the chosen cases as one experiment, and return our own records of the runs as well."""
    sync_dataset(client, cases)
    wanted = {case.id for case in cases}
    items = [item for item in client.get_dataset(DATASET_NAME).items if item.id in wanted]
    live = LiveEval(cases, make_llm=make_llm, make_embedder=make_embedder, judge=judge, retries=retries,
                    make_pool=make_pool, on_result=on_result)
    result = client.run_experiment(
        name=DATASET_NAME, run_name=label, data=items, task=live.task, evaluators=[live.evaluator],
        run_evaluators=live.run_evaluators(), max_concurrency=1, metadata=metadata,
    )
    return [live.results[case.id] for case in cases if case.id in live.results], live.now, result
