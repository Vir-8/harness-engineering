"""Runs eval cases through the real harness and records what happened.

Each case gets a clean slate (the refund tables are emptied: they are scratch data in this dev database) and a pinned
clock, so "delivered 4 days ago" means the same thing today and next month. The model, the embeddings and the database
are whatever is passed in: real ones for an eval, fakes in the tests.
"""

import json
import subprocess
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from opentelemetry import trace
from psycopg_pool import AsyncConnectionPool

from refund_harness.agent.errors import ModelError
from refund_harness.agent.llm import LLMClient
from refund_harness.agent.loop import MAX_STEPS, run_agent
from refund_harness.agent.types import LLMResponse, Message, ToolSpec
from refund_harness.data.repository import get_order_row, search_policy_by_vector
from refund_harness.evals.cases import EvalCase
from refund_harness.evals.judge import Judge
from refund_harness.evals.results import CaseRun, Retrieval, ToolRun
from refund_harness.evals.scorers import score_run
from refund_harness.refund_tools import build_registry
from refund_harness.retrieval.embedder import Embedder

tracer = trace.get_tracer("evals")

EVAL_CLOCK_OFFSET = timedelta(days=4)  # eval "now" = when acme's order A100 was delivered, plus this
RETRIEVAL_DEPTH = 3
REPO_ROOT = Path(__file__).resolve().parents[3]


async def eval_now(pool: AsyncConnectionPool) -> datetime:
    """The pinned clock. The seed dates are relative to when the database was created, so derive it from the data:
    at this moment acme's A100 is 4 days old and A200 is 46 days old, whatever today's date is."""
    row = await get_order_row(pool, "acme", "A100")
    return row["delivered_at"] + EVAL_CLOCK_OFFSET


class RecordingLLM:
    """Wraps the real model to note every call: what it answered (with its tokens and cost) and how long it took."""

    def __init__(self, inner: LLMClient):
        self._inner = inner
        self.calls: list[tuple[LLMResponse, float]] = []

    async def complete(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        started = time.perf_counter()
        response = await self._inner.complete(messages, tools)
        self.calls.append((response, time.perf_counter() - started))
        return response


async def reset_refunds(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        await conn.execute("TRUNCATE TABLE refund_requests, refund_events")


async def refund_amounts(pool: AsyncConnectionPool) -> list[int]:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT amount_cents FROM refund_requests ORDER BY id")
        return [row["amount_cents"] for row in await cur.fetchall()]


def tool_runs(messages: list[Message]) -> list[ToolRun]:
    """Every tool call the model made, paired with what came back."""
    outputs = {m.tool_call_id: m.content for m in messages if m.role == "tool"}
    return [
        ToolRun(
            name=call.name,
            arguments=call.arguments,
            output=outputs.get(call.id, ""),
            crashed=outputs.get(call.id, "").startswith("Tool crashed mid-run"),
        )
        for m in messages
        if m.role == "assistant"
        for call in m.tool_calls
    ]


def count_repeated_calls(tools: list[ToolRun]) -> int:
    """How many tool calls repeated an earlier one exactly (same tool, same arguments): the model retrying blindly."""
    seen: set[tuple[str, str]] = set()
    repeated = 0
    for tool in tools:
        key = (tool.name, json.dumps(tool.arguments, sort_keys=True))
        repeated += key in seen
        seen.add(key)
    return repeated


async def measure_retrieval(case: EvalCase, pool: AsyncConnectionPool, embedder: Embedder) -> Retrieval | None:
    """Plain vector search on the customer's own words: where does the expected chunk land? No model beyond the embedding."""
    if not case.expected_chunk:
        return None
    vector = await embedder.embed_query(case.question)
    top = await search_policy_by_vector(pool, case.merchant, vector, RETRIEVAL_DEPTH)
    titles = [row["title"] for row in top]
    rank = titles.index(case.expected_chunk) + 1 if case.expected_chunk in titles else None
    return Retrieval(
        rank=rank,
        distance=top[rank - 1]["distance"] if rank else None,
        best_distance=top[0]["distance"] if top else None,
        top_titles=titles,
    )


async def run_case(
    case: EvalCase,
    *,
    pool: AsyncConnectionPool,
    llm: LLMClient,
    embedder: Embedder,
    now: datetime,
    repeat: int = 1,
    judge: Judge | None = None,
    retries: int = 1,
) -> CaseRun:
    """Run one case. A failed MODEL CALL (the CLI died, a timeout) is infrastructure, not the agent being wrong, so
    it is retried up to `retries` times; `attempts` records how many tries it took. Anything else is the agent's run."""
    registry = build_registry(pool, case.merchant, embedder, now=lambda: now)
    run = CaseRun(case_id=case.id, category=case.category, merchant=case.merchant, question=case.question, repeat=repeat)

    started = time.perf_counter()
    for attempt in range(1, retries + 2):
        await reset_refunds(pool)  # a clean slate, also between attempts
        recorder = RecordingLLM(llm)
        run.attempts, run.error = attempt, None
        try:
            with tracer.start_as_current_span("eval_case") as span:  # lets a trace be traced back to its case
                span.set_attribute("eval.case_id", case.id)
                span.set_attribute("eval.repeat", repeat)
                span.set_attribute("eval.attempt", attempt)
                result = await run_agent(recorder, registry, case.question, max_steps=MAX_STEPS)
            run.answer, run.stop_reason, run.tools = result.final_text, result.stop_reason, tool_runs(result.messages)
            break
        except ModelError as error:
            run.error = f"{type(error).__name__}: {error}"
            if attempt > retries:
                break
        except Exception as error:  # one failed case must not end the whole run, and a bug is not retried
            run.error = f"{type(error).__name__}: {error}"
            break
    run.latency_s = time.perf_counter() - started

    run.repeated_calls = count_repeated_calls(run.tools)
    run.model_calls = len(recorder.calls)
    usages = [response.usage for response, _ in recorder.calls if response.usage]
    run.input_tokens = sum(u.input_tokens for u in usages)
    run.output_tokens = sum(u.output_tokens for u in usages)
    run.cost_usd = round(sum(u.cost_usd or 0 for u in usages), 6)
    run.refunds_cents = await refund_amounts(pool)

    try:
        run.retrieval = await measure_retrieval(case, pool, embedder)
    except Exception as error:
        run.error = run.error or f"retrieval check failed: {type(error).__name__}: {error}"

    if judge is not None and run.error is None:
        try:
            run.judge, judge_usage = await judge.grade(case.question, run.tools, run.answer, registry.specs())
            run.judge_cost_usd = round((judge_usage.cost_usd or 0) if judge_usage else 0, 6)
        except Exception as error:  # a broken judge call is reported, not counted as the agent failing
            run.judge_error = f"{type(error).__name__}: {error}"

    run.scores = score_run(case, run)
    return run


async def run_cases(
    cases: list[EvalCase],
    *,
    repeats: int,
    pool: AsyncConnectionPool,
    llm: LLMClient,
    embedder: Embedder,
    now: datetime,
    judge: Judge | None = None,
    retries: int = 1,
    on_result: Callable[[CaseRun], None] | None = None,
) -> list[CaseRun]:
    """One at a time: every case empties and then reads the shared refund tables, so they cannot overlap."""
    runs = []
    for repeat in range(1, repeats + 1):
        for case in cases:
            run = await run_case(
                case, pool=pool, llm=llm, embedder=embedder, now=now, repeat=repeat, judge=judge, retries=retries
            )
            runs.append(run)
            if on_result:
                on_result(run)
    return runs


def git_state() -> tuple[str, bool]:
    """The commit the code was at and whether there were uncommitted changes: which version produced these numbers."""
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False).stdout.strip()

    return git("rev-parse", "--short", "HEAD") or "unknown", bool(git("status", "--porcelain"))
