// The Architecture page's content: every part of the system, and what can go wrong.
// Each claim here was checked against the code (file paths are relative to the repository root).

export type Part = { id: string; name: string; tech: string; what: string; source: string };

export const PARTS: Part[] = [
  {
    id: "console",
    name: "Console",
    tech: "Next.js",
    what: "The operator console you are looking at. It calls the API from its own server, so the API's address and key never reach the browser.",
    source: "frontend/src/lib/api.ts",
  },
  {
    id: "api",
    name: "API",
    tech: "FastAPI",
    what: "Takes the merchant from the request, applies a per-merchant rate limit, then runs the agent. Approving and rejecting are separate endpoints that record who did it.",
    source: "src/refund_harness/app.py",
  },
  {
    id: "loop",
    name: "Agent loop",
    tech: "Our own code, 117 lines",
    what: "Calls the model, runs the tools it asks for, and repeats, for at most 5 rounds. Unknown tools, bad arguments and tool crashes go back to the model as results. Every step is a trace span with its tokens and cost.",
    source: "src/refund_harness/agent/loop.py",
  },
  {
    id: "model",
    name: "Model",
    tech: "OpenAI · gpt-6-luna",
    what: "The OpenAI Responses API by default, behind one interface with a Claude adapter and a fake for tests. Changing provider means changing one adapter.",
    source: "src/refund_harness/adapters/",
  },
  {
    id: "tools",
    name: "Tools",
    tech: "get_order · search_policy · submit_order_refund",
    what: "The only things the model can do. Submitting checks eligibility in code, inside the transaction, and creates a pending request: there is no tool to approve or pay.",
    source: "src/refund_harness/refund_tools.py",
  },
  {
    id: "postgres",
    name: "Postgres",
    tech: "Postgres 17 · pgvector",
    what: "The source of truth: orders, refund requests, the audit log, the outbox, and the policy text with its embeddings for vector search.",
    source: "src/refund_harness/data/repository.py",
  },
  {
    id: "redis",
    name: "Redis",
    tech: "Redis 8",
    what: "Caches the embedding of each question (674 ms down to 0.5 ms) and counts requests for the rate limit. Both work without it.",
    source: "src/refund_harness/cache/",
  },
  {
    id: "neo4j",
    name: "Neo4j",
    tech: "Neo4j Community",
    what: "A copy of who shares a card or an address. When a request is filed it finds customers within two steps, and the notes go to the reviewer, never to the model. At two steps SQL is as fast; at seven the graph answers in 67 ms against 706 ms (50,000 customers).",
    source: "src/refund_harness/graph/risk.py",
  },
  {
    id: "reviewer",
    name: "Reviewer",
    tech: "A person, in the console",
    what: "Reads the request with the customer's history and any linked accounts, then approves it or rejects it with a reason. Both are recorded with who did it.",
    source: "frontend/src/app/approvals/page.tsx",
  },
  {
    id: "approval",
    name: "Approval",
    tech: "One Postgres transaction",
    what: "A reviewer's approval writes the new status, an audit event and the payout job together: all three or none.",
    source: "src/refund_harness/data/repository.py",
  },
  {
    id: "relay",
    name: "Relay",
    tech: "Transactional outbox",
    what: "Forwards committed jobs to the queue and marks them sent. At least once: a job can be sent twice, never lost.",
    source: "src/refund_harness/jobs/relay.py",
  },
  {
    id: "queue",
    name: "Queue",
    tech: "SQS · dead-letter queue",
    what: "A job that fails three times moves to a dead-letter queue and raises an alert, instead of retrying forever. ElasticMQ stands in for SQS locally.",
    source: "src/refund_harness/jobs/sqs.py",
  },
  {
    id: "worker",
    name: "Worker",
    tech: "Python",
    what: "Pays with an idempotency key named after the refund, records it as issued, and deletes the message only after that.",
    source: "src/refund_harness/payments/payout.py",
  },
  {
    id: "provider",
    name: "Payment provider",
    tech: "Stand-in, Stripe-like",
    what: "Its own process with its own ledger. Sent the same key twice, it returns the first payout instead of paying again.",
    source: "src/refund_harness/payments/fake_provider.py",
  },
  {
    id: "reconcile",
    name: "Reconciliation",
    tech: "Ours against theirs",
    what: "Matches every payout with ours by reference: anything paid twice, never paid or paid the wrong amount shows on the Ledger page.",
    source: "src/refund_harness/payments/reconcile.py",
  },
  {
    id: "traces",
    name: "Traces",
    tech: "OpenTelemetry · Sentry",
    what: "One trace per request with the model calls, tool calls and database queries, their tokens and cost. Errors go to Sentry.",
    source: "src/refund_harness/observability.py",
  },
  {
    id: "metrics",
    name: "Metrics",
    tech: "Prometheus · Grafana",
    what: "Requests, latency, model spend and queue depth, with alerts when payouts reach the dead-letter queue or wait too long.",
    source: "monitoring/",
  },
  {
    id: "evals",
    name: "Evals",
    tech: "Scored in code · Langfuse",
    what: "Hand-labelled cases run through the real harness. A run passes on what the agent did, not on how it sounded. Results on the Quality page.",
    source: "src/refund_harness/evals/",
  },
  {
    id: "load",
    name: "Load",
    tech: "k6",
    what: "Simulated customers against one API process with a stand-in model, to measure the harness rather than the model provider. At 1,500 at once: p95 5.1 s, no errors. The limit is one CPU core, so it scales by adding processes.",
    source: "load/ask.js",
  },
];

export type Failure = { failure: string; guard: string };

// Each guard has a test that makes the failure happen (tests/ in the repository).
export const FAILURES: Failure[] = [
  { failure: "The same order refunded twice", guard: "One live request per order (unique index); the order row is locked while checking" },
  { failure: "The model acts for another merchant", guard: "The merchant is bound when the tools are built, never a tool argument" },
  { failure: "The model approves or pays", guard: "No tool for either" },
  { failure: "A customer insists on an ineligible refund", guard: "Eligibility is checked in code, whatever the model was told" },
  { failure: "Approved, but the payout job is lost", guard: "The job is written in the approval's transaction (outbox)" },
  { failure: "The worker dies after the provider paid", guard: "The retry sends the same idempotency key; the provider returns the first payout" },
  { failure: "Two workers pay the same refund", guard: "Same key, and only an approved refund can be marked issued" },
  { failure: "The provider is down, or a job is malformed", guard: "Retries with backoff, then the dead-letter queue and an alert" },
  { failure: "A payout made outside the system", guard: "Reconciliation against the provider's ledger" },
  { failure: "Accounts sharing a card farm refunds", guard: "Linked-account notes for the reviewer, never shown to the model" },
  { failure: "The model makes up a policy", guard: "Weak policy matches are dropped and the model is told not to guess" },
  { failure: "One merchant floods the agent", guard: "A rate limit per merchant in Redis" },
];

export const STACK: [string, string[]][] = [
  ["Backend", ["Python 3.12", "FastAPI", "Pydantic", "psycopg 3, plain SQL"]],
  ["Data", ["Postgres 17", "pgvector", "Neo4j", "Redis 8"]],
  ["Jobs", ["Transactional outbox", "SQS", "Dead-letter queue"]],
  ["Models", ["OpenAI Responses API", "Claude", "Gemini embeddings"]],
  ["Observability", ["OpenTelemetry", "Sentry", "Prometheus", "Grafana", "Langfuse"]],
  ["Testing", ["pytest on real services", "k6"]],
  ["Console", ["Next.js 16", "React 19", "Tailwind CSS 4"]],
  ["Runs on", ["Docker Compose"]],
];
