# refund-harness

Learning project: an agent harness for an e-commerce support and refund agent, built phase by phase.

## Layout

```
src/refund_harness/
  app.py            FastAPI app and composition root: endpoints (ask the agent, human approval), dependencies
  schemas.py        HTTP request/response models
  refund_tools.py   the agent's tools: get_order, search_policy, submit_order_refund (per-merchant, built per request)
  observability.py  Sentry errors + OpenTelemetry traces (console tree, GenAI spans, tokens/cost), all switchable
  agent/            the harness core, provider-neutral
    loop.py           the agent loop (run_agent), with its spans
    tools.py          Tool / ToolRegistry (Pydantic validates tool arguments)
    types.py          Message, ToolCall, LLMResponse, Usage, RunResult, ...
    llm.py            LLMClient protocol + test doubles (FakeLLM, RaisingLLM)
    pricing.py        cost of a model call from token counts and the PUBLISHED prices (the CLI's own cost figure is model-blind)
    errors.py         model-call failures, raised by adapters, mapped to HTTP by the API
  adapters/         one file per outside provider
    claude_cli.py     the model, via the `claude` CLI (lean context, pinned model, usage parsing)
    gemini_embeddings.py   embeddings, via the google-genai SDK
  retrieval/        turning text into vectors
    embedder.py       Embedder protocol + FakeEmbedder
    ingest.py         offline job: embed policy chunks and store the vectors
  evals/            how good is the agent? cases, runner, scorers, judge, reports, Langfuse sink
    cases.py          the hand-labelled cases (loaded from evals/cases.yaml, validated)
    runner.py         runs a case through the real harness at a pinned clock and records everything
    scorers.py        pure-code checks: tools, words, retrieval hit@k, database state
    judge.py          LLM judge: evidence support and abstention
    judge_check.py    labelling sheet + agreement between the judge and a human
    report.py         summaries, comparing two runs, saving results
    langfuse_sink.py  runs the cases as a Langfuse experiment
  data/             Postgres
    db.py             connection pool
    repository.py     all SQL: orders, refund requests/approval/eligibility, policy search (keyword + vector)
db/init/            schema + seed, run in order by Postgres on a FRESH volume only (apply later files by hand otherwise)
scripts/
  try_it.py         run one real question through the whole harness and print every step (needs .env)
  ingest_policies.py  embed the policy chunks with the real Gemini model (needs .env)
  compare_search.py keyword vs semantic search on sample questions, with distances (needs .env)
  try_embedder.py   one live call to the Gemini embedder
  check_trace_export.py  proves traces leave the process (local collector; no Sentry, no network)
  run_evals.py      run the eval cases for real (--langfuse also stores them as a Langfuse experiment)
  compare_evals.py  compare two saved eval runs
  judge_sheet.py / judge_agreement.py   label the judge's verdicts yourself, then measure how often it agrees with you
  demo_server.py    blocking-vs-async experiment server
  load.py           concurrent load + /health probe
evals/cases.yaml    the eval cases (human labels); evals/judge_labels.yaml your labels of the judge; evals/results/ saved runs (git-ignored)
tests/              one file per topic; helpers.py + conftest.py hold the shared pieces
```

## Setup

```
docker compose up -d                 # Postgres + pgvector on port 5433
printf 'GEMINI_API_KEY=...\nSENTRY_DSN=...\nLANGFUSE_PUBLIC_KEY=...\nLANGFUSE_SECRET_KEY=...\nLANGFUSE_HOST=https://cloud.langfuse.com\n' > .env   # secrets; .env is git-ignored. Without SENTRY_DSN, Sentry stays off
```

## Commands

```
uv run pytest -q                                         # tests (need the database; no network, no tokens)
uv run ruff check --select F,I src tests scripts         # unused imports, import order
uv run --env-file .env uvicorn refund_harness.app:app --reload   # the API; docs at /docs; each request's trace prints as a tree (TRACING=off disables)
uv run --env-file .env python scripts/try_it.py acme "Can I get a refund for A100?"
uv run --env-file .env python scripts/ingest_policies.py          # after adding policy chunks
```
