# refund-harness

Learning project: an agent harness for an e-commerce support and refund agent, built phase by phase.

## Layout

```
src/refund_harness/
  types.py          provider-neutral data shapes (Message, ToolCall, RunResult, ...)
  llm.py            LLMClient protocol + test doubles (FakeLLM, RaisingLLM)
  embedder.py       Embedder protocol + FakeEmbedder (text -> vector)
  errors.py         model-call failures, raised by adapters, mapped to HTTP by the API
  tools.py          Tool / ToolRegistry (Pydantic validates tool arguments)
  refund_tools.py   the agent's tools: get_order, search_policy, submit_order_refund (per-merchant, built per request)
  repository.py     all SQL: orders, refund requests/approval/eligibility, policy search (keyword + vector)
  ingest.py         offline job: embed policy chunks and store the vectors
  db.py             Postgres connection pool
  loop.py           the agent loop (run_agent)
  adapters/         one adapter per provider (claude_cli.py for the model, gemini_embeddings.py for embeddings)
  schemas.py        HTTP request/response models
  app.py            FastAPI app: endpoints (ask the agent, human approval), dependencies, error translation
  observability.py  Sentry errors + OpenTelemetry traces (console tree, GenAI spans, tokens/cost), all switchable
db/init/            schema + seed, run in order by Postgres on a FRESH volume only (apply later files by hand otherwise)
scripts/
  try_it.py         run one real question through the whole harness and print every step (needs .env)
  ingest_policies.py  embed the policy chunks with the real Gemini model (needs .env)
  compare_search.py keyword vs semantic search on sample questions, with distances (needs .env)
  try_embedder.py   one live call to the Gemini embedder
  check_trace_export.py  proves traces leave the process (local collector; no Sentry, no network)
  demo_server.py    blocking-vs-async experiment server
  load.py           concurrent load + /health probe
tests/              one file per topic; helpers.py + conftest.py hold the shared pieces
```

## Setup

```
docker compose up -d                 # Postgres + pgvector on port 5433
printf 'GEMINI_API_KEY=...\nSENTRY_DSN=...\n' > .env   # secrets; .env is git-ignored. Without SENTRY_DSN, Sentry stays off
```

## Commands

```
uv run pytest -q                                         # tests (need the database; no network, no tokens)
uv run ruff check --select F,I src tests scripts         # unused imports, import order
uv run --env-file .env uvicorn refund_harness.app:app --reload   # the API; docs at /docs; each request's trace prints as a tree (TRACING=off disables)
uv run --env-file .env python scripts/try_it.py acme "Can I get a refund for A100?"
uv run --env-file .env python scripts/ingest_policies.py          # after adding policy chunks
```
