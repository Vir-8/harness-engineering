"""Runs the eval cases through the REAL harness (real model, real embeddings, real database) and saves a report.

    uv run --env-file .env python scripts/run_evals.py --label baseline
    uv run --env-file .env python scripts/run_evals.py --cases policy-acme-return-window,refund-too-old --no-judge
    uv run --env-file .env python scripts/run_evals.py --category refund --repeat 3 --model gpt-6.1-sol --label sol
    uv run --env-file .env python scripts/run_evals.py --provider claude --model claude-haiku-5-5 --label haiku
    uv run --env-file .env python scripts/run_evals.py --langfuse --label sonnet     # also stored as a Langfuse experiment

Costs real model calls (each case is 2-3 agent calls, plus one judge call): start with --cases or --category.
It EMPTIES the refund tables before every case: they are scratch data in the dev database.
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime

from refund_harness.adapters.factory import (
    PROVIDERS,
    create_llm,
    default_model,
    resolve_provider,
)
from refund_harness.adapters.gemini_embeddings import DEFAULT_MODEL as EMBEDDING_MODEL
from refund_harness.adapters.gemini_embeddings import GeminiEmbedder
from refund_harness.data.db import create_pool
from refund_harness.evals.cases import EvalCase, load_cases
from refund_harness.evals.judge import Judge
from refund_harness.evals.report import render_summary, save_report
from refund_harness.evals.results import CaseRun, RunReport
from refund_harness.evals.runner import eval_now, git_state, reset_refunds, run_cases

DEFAULT_JUDGE_MODEL = "claude-opus-4-6"  # deliberately not the agent's model: a model grading its own work is lenient


def progress(run: CaseRun) -> None:
    failed = [name for name, value in run.scores.items() if value is False]
    verdict = "PASS" if run.passed else "FAIL"
    extra = f"  failed: {', '.join(failed)}" if failed else ""
    extra += f"  needed {run.attempts} attempts" if run.attempts > 1 else ""
    extra += f"  ERROR: {run.error}" if run.error else ""
    extra += f"  judge error: {run.judge_error}" if run.judge_error else ""
    print(f"{verdict}  {run.case_id:36} (repeat {run.repeat})  {run.latency_s:5.1f}s  ${run.cost_usd:.3f}{extra}", flush=True)


def select_cases(args: argparse.Namespace) -> list[EvalCase] | None:
    cases = load_cases()
    if args.cases:
        wanted = set(args.cases.split(","))
        unknown = wanted - {case.id for case in cases}
        if unknown:
            print(f"unknown case ids: {sorted(unknown)}")
            return None
        cases = [case for case in cases if case.id in wanted]
    if args.category:
        cases = [case for case in cases if case.category == args.category]
    if not cases:
        print("no cases selected")
        return None
    return cases


def judge_for(args: argparse.Namespace) -> Judge | None:
    return None if args.no_judge else Judge.with_claude(args.judge_model)


def finish(args: argparse.Namespace, runs: list[CaseRun], now: datetime, started: str, footer: str = "") -> int:
    sha, dirty = git_state()
    report = RunReport(
        label=args.label, started_at=started, git_sha=sha, git_dirty=dirty, agent_model=args.model,
        judge_model=None if args.no_judge else args.judge_model, embedding_model=EMBEDDING_MODEL,
        eval_now=now.isoformat(), runs=runs,
    )
    path = save_report(report)
    print("\n" + render_summary(report) + f"\n\nsaved: {path}{footer}")
    return 0


async def run_locally(args: argparse.Namespace, cases: list[EvalCase]) -> int:
    pool = await create_pool()
    started = datetime.now().replace(microsecond=0).isoformat()
    try:
        now = await eval_now(pool)
        judge = judge_for(args)
        print(f"{len(cases)} cases x {args.repeat} repeat(s), agent {args.model}, judge {judge.model if judge else 'none'}, clock {now:%Y-%m-%d}\n", flush=True)
        runs = await run_cases(
            cases, repeats=args.repeat, pool=pool, llm=create_llm(args.provider, args.model),
            embedder=GeminiEmbedder(), now=now, judge=judge, retries=args.retries, on_result=progress,
        )
        await reset_refunds(pool)
    finally:
        await pool.close()
    return finish(args, runs, now, started)


def run_in_langfuse_experiment(args: argparse.Namespace, cases: list[EvalCase]) -> int:
    """Plain synchronous code on purpose: Langfuse runs the cases on an event loop of its own (see langfuse_sink)."""
    from langfuse import get_client

    from refund_harness.evals.langfuse_sink import run_in_langfuse

    client = get_client()
    if not client.auth_check():
        print("Langfuse authentication failed: check LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY and LANGFUSE_HOST in .env")
        return 2
    started = datetime.now().replace(microsecond=0).isoformat()
    judge = judge_for(args)
    print(f"{len(cases)} cases as a Langfuse experiment, agent {args.model}, judge {judge.model if judge else 'none'}\n", flush=True)
    sha, dirty = git_state()
    runs, now, result = run_in_langfuse(
        client, cases, label=args.label, make_llm=lambda: create_llm(args.provider, args.model), make_embedder=GeminiEmbedder,
        judge=judge, retries=args.retries, on_result=progress,
        metadata={"agent_model": args.model, "judge_model": args.judge_model if judge else "none", "git_sha": sha + ("+dirty" if dirty else ""),
                  "embedding_model": EMBEDDING_MODEL},
    )
    client.flush()

    async def clean_up():  # the experiment closed its pool with the last case; this leaves the refund tables empty
        pool = await create_pool()
        await reset_refunds(pool)
        await pool.close()

    asyncio.run(clean_up())
    link = getattr(result, "experiment_url", None)
    return finish(args, runs, now, started, footer=f"\nLangfuse: {link}" if link else "")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--label", default="run", help="a name for this run, used in the results file name and as the Langfuse run name")
    parser.add_argument("--cases", help="comma-separated case ids (default: all)")
    parser.add_argument("--category", help="only this category")
    parser.add_argument("--repeat", type=int, default=1, help="run each case this many times, to see variance")
    parser.add_argument("--provider", choices=PROVIDERS, help="who serves the agent's model (default: LLM_PROVIDER, else openai)")
    parser.add_argument("--model", help="the agent's model (default: the provider's default, see adapters/factory.py)")
    parser.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL)
    parser.add_argument("--no-judge", action="store_true", help="code scorers only: cheaper and faster")
    parser.add_argument("--retries", type=int, default=1, help="re-run a case this many times if the MODEL CALL fails (infrastructure, not the agent)")
    parser.add_argument("--langfuse", action="store_true", help="also run as a Langfuse experiment (needs the LANGFUSE_* keys in .env)")
    arguments = parser.parse_args()
    arguments.provider = resolve_provider(arguments.provider)
    arguments.model = arguments.model or default_model(arguments.provider)  # so reports name the model that really ran
    if not os.environ.get("GEMINI_API_KEY"):
        sys.exit("GEMINI_API_KEY is not set: run with `uv run --env-file .env python scripts/run_evals.py ...`")
    if arguments.langfuse and arguments.repeat > 1:
        sys.exit("--repeat is not supported with --langfuse: a dataset item runs once per experiment run")
    selected = select_cases(arguments)
    if selected is None:
        sys.exit(2)
    sys.exit(run_in_langfuse_experiment(arguments, selected) if arguments.langfuse else asyncio.run(run_locally(arguments, selected)))
