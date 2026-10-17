# refund-harness

Learning project: an agent harness for an e-commerce support and refund agent, built phase by phase.

## Layout

```
src/refund_harness/
  types.py          provider-neutral data shapes (Message, ToolCall, RunResult, ...)
  llm.py            LLMClient protocol + test doubles (FakeLLM, RaisingLLM)
  errors.py         model-call failures, raised by adapters, mapped to HTTP by the API
  tools.py          Tool / ToolRegistry (Pydantic validates tool arguments)
  refund_tools.py   the refund agent's tools (fake in-memory data until Phase 2)
  loop.py           the agent loop (run_agent)
  adapters/         one adapter per model provider (claude_cli.py)
  schemas.py        HTTP request/response models
  app.py            FastAPI app: endpoints, dependencies, error translation
scripts/
  try_it.py         run one real question and print every step
  demo_server.py    blocking-vs-async experiment server
  load.py           concurrent load + /health probe
```

## Commands

```
uv run pytest -q                                         # tests (no network, no tokens)
uv run ruff check --select F,I src tests scripts         # unused imports, import order
uv run uvicorn refund_harness.app:app --reload           # the API; docs at /docs
uv run python scripts/try_it.py "Can I get a refund for A100?"
```
