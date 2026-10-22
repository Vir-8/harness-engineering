"""Prometheus metrics: numbers about the whole system over time, as opposed to one request (a trace) or one failure (Sentry).

Each process keeps its numbers in memory and shows them as text at /metrics; Prometheus fetches ("scrapes") that page
every few seconds and stores the history. The API serves /metrics itself; the worker runs a tiny server on its own port.

LABELS split a metric (by route, by outcome...). Every distinct label combination is a separate time series, so a label
must only ever take a few values: a route TEMPLATE such as /refunds/{refund_id}/approve, never the raw path with the id
in it, and never merchant ids, refund ids or anything else that grows with the data ("cardinality").
"""

from prometheus_client import Counter, Gauge, Histogram

from refund_harness.agent.types import Usage

# Most requests are fast (database reads); asks take seconds (model calls), so the buckets reach well past 10 s.
LATENCY_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 40, 80)

# --- HTTP (the API) ---
HTTP_REQUESTS = Counter("http_requests", "Requests handled", ["method", "route", "status"])
HTTP_DURATION = Histogram("http_request_duration_seconds", "Time to answer a request", ["method", "route"], buckets=LATENCY_BUCKETS)

# --- the agent ---
AGENT_RUNS = Counter("agent_runs", "Agent runs finished", ["stop_reason"])  # final | max_steps | error
AGENT_DURATION = Histogram("agent_run_duration_seconds", "Time for one whole agent run", buckets=LATENCY_BUCKETS)
MODEL_CALLS = Counter("llm_calls", "Model calls", ["model", "outcome"])  # outcome: ok | error
MODEL_DURATION = Histogram("llm_call_duration_seconds", "Time for one model call", ["model"], buckets=LATENCY_BUCKETS)
MODEL_TOKENS = Counter("llm_tokens", "Tokens used", ["model", "direction"])  # direction: input | output
MODEL_COST = Counter("llm_cost_usd", "Cost of model calls at published prices (US dollars)", ["model"])
TOOL_CALLS = Counter("agent_tool_calls", "Tool calls the model made", ["tool", "outcome"])  # ok | invalid_arguments | crashed | unknown_tool

# --- the embedding cache ---
EMBEDDING_CACHE = Counter("embedding_cache_lookups", "Question-vector cache lookups", ["outcome"])  # hit | miss | error

# --- payouts (the worker) ---
PAYOUT_JOBS = Counter("payout_jobs", "Payout jobs handled", ["outcome"])  # issued | replayed | nothing_to_do | failed
PAYOUT_JOB_WAIT = Histogram(
    "payout_job_wait_seconds",
    "Time from a job entering the queue to a worker picking it up (each delivery, retries included)",
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600, 1800),
)
QUEUE_MESSAGES = Gauge("payout_queue_messages", "Messages on the payout queues", ["queue", "state"])  # payouts|dead_letters, waiting|in_flight

UNKNOWN_MODEL = "unknown"


def record_model_call(usage: Usage | None, seconds: float, *, failed: bool = False) -> None:
    model = (usage and usage.model_name) or UNKNOWN_MODEL
    MODEL_CALLS.labels(model, "error" if failed else "ok").inc()
    MODEL_DURATION.labels(model).observe(seconds)
    if usage is None:
        return
    MODEL_TOKENS.labels(model, "input").inc(usage.input_tokens)
    MODEL_TOKENS.labels(model, "output").inc(usage.output_tokens)
    if usage.cost_usd is not None:
        MODEL_COST.labels(model).inc(usage.cost_usd)
