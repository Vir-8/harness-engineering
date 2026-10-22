"""HTTP layer. The agent logic lives elsewhere; this file only translates HTTP <-> harness."""

import asyncio
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from functools import lru_cache

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
)
from fastapi.responses import JSONResponse
from neo4j import AsyncDriver
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from psycopg_pool import AsyncConnectionPool
from redis.asyncio import Redis

from refund_harness import metrics
from refund_harness.adapters.factory import create_llm
from refund_harness.adapters.gemini_embeddings import GeminiEmbedder
from refund_harness.agent.errors import (
    ModelCallError,
    ModelTimeoutError,
    ReplyParseError,
)
from refund_harness.agent.llm import LLMClient
from refund_harness.agent.loop import run_agent
from refund_harness.agent.tools import ToolRegistry
from refund_harness.cache.client import create_redis
from refund_harness.cache.embedding_cache import CachedEmbedder
from refund_harness.cache.rate_limit import RateLimiter
from refund_harness.data.db import create_pool
from refund_harness.data.repository import (
    approve_refund,
    get_customer,
    get_refund_detail,
    list_activity,
    list_refunds,
    refund_status_counts,
    reject_refund,
)
from refund_harness.demo import reset_demo
from refund_harness.graph.driver import create_graph_driver
from refund_harness.jobs.sqs import (
    PAYOUTS_QUEUE,
    create_sqs_client,
    dead_letter_queue_name,
    queue_counts,
)
from refund_harness.observability import enable_tracing, init_sentry
from refund_harness.payments.client import PaymentError, PaymentProvider
from refund_harness.payments.reconcile import reconcile
from refund_harness.refund_tools import build_registry
from refund_harness.retrieval.embedder import Embedder
from refund_harness.schemas import (
    ActivityItem,
    ApproveResponse,
    AskRequest,
    AskResponse,
    CustomerDetail,
    Ledger,
    LedgerLineOut,
    OpsSummary,
    QueueDepth,
    RefundDetail,
    RefundStatus,
    RefundSummary,
    RejectRequest,
    RejectResponse,
)

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await create_pool()
    app.state.graph_driver = create_graph_driver()  # creating a driver does not connect: Neo4j being down cannot stop startup
    app.state.redis = create_redis()  # same for Redis: it connects on first use, and every use tolerates it being gone
    app.state.provider = PaymentProvider.from_env()  # read-only here (the ledger view); only the worker pays
    app.state.sqs = create_sqs_client()  # read-only here too (queue depths); only the worker's relay sends
    yield
    await app.state.provider.aclose()
    await app.state.redis.aclose()
    await app.state.graph_driver.close()
    await app.state.pool.close()


init_sentry()  # before the app exists: Sentry hooks into the framework as it starts. Off unless SENTRY_DSN is set.
app = FastAPI(title="Agent harness for financial operations", lifespan=lifespan)
enable_tracing(app)  # prints each request as a tree of spans; TRACING=off disables


# Every route but these needs the key, when one is configured. A public deployment must set it: the API spends model
# credits and moves money, and X-Merchant-Id / X-Reviewer-Id are stand-ins that anyone could send.
OPEN_PATHS = {"/health", "/metrics"}


@app.middleware("http")
async def require_api_key(request: Request, call_next):
    """HARNESS_API_KEY set: every request must carry `Authorization: Bearer <key>`. Unset (local development): open."""
    expected = os.environ.get("HARNESS_API_KEY")
    if expected and request.url.path not in OPEN_PATHS:
        given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not secrets.compare_digest(given.encode(), expected.encode()):  # constant time: no guessing it byte by byte
            return JSONResponse(status_code=401, content={"detail": "Missing or wrong API key."})
    return await call_next(request)


@app.middleware("http")
async def count_requests(request: Request, call_next):
    """Every request, counted and timed per route for Prometheus. /metrics itself is left out (it is read every 5 s)."""
    if request.url.path == "/metrics":
        return await call_next(request)
    started = time.perf_counter()
    status = 500  # unless a response comes back: an unhandled exception still counts, as a server error
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        # The route TEMPLATE (/refunds/{refund_id}/approve), set by the router once it matched. Unmatched paths (404s
        # for made-up URLs) share one label, or anyone could create unlimited time series just by requesting URLs.
        route = getattr(request.scope.get("route"), "path", "unmatched")
        metrics.HTTP_REQUESTS.labels(request.method, route, str(status)).inc()
        metrics.HTTP_DURATION.labels(request.method, route).observe(time.perf_counter() - started)


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics() -> Response:
    """This process's numbers in Prometheus's text format. Exempt from the API key (OPEN_PATHS) so Prometheus can scrape
    it: keep the port off the public network."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# Dependencies: endpoints ask for these with Depends(...), so tests can swap them out.
@lru_cache
def get_llm() -> LLMClient:
    # LLM_PROVIDER picks it: openai (default), claude, or fake for load tests (adapters/factory.py). One instance for
    # the process, so the OpenAI client's connection pool is shared by every request instead of rebuilt for each.
    return create_llm()


@lru_cache
def _gemini_embedder() -> GeminiEmbedder:
    return GeminiEmbedder()  # one shared instance, so one SDK client for the whole process


def get_redis(request: Request) -> Redis | None:
    return request.app.state.redis


def get_embedder(redis: Redis | None = Depends(get_redis)) -> Embedder:
    embedder = _gemini_embedder()
    return CachedEmbedder(embedder, redis) if redis is not None else embedder


def get_pool(request: Request) -> AsyncConnectionPool:
    return request.app.state.pool


def get_graph_driver(request: Request) -> AsyncDriver | None:
    return request.app.state.graph_driver


def get_provider(request: Request) -> PaymentProvider:
    return request.app.state.provider


def get_sqs(request: Request):
    return request.app.state.sqs


def get_merchant_id(x_merchant_id: str = Header(min_length=1, max_length=64)) -> str:
    return x_merchant_id


def get_reviewer_id(x_reviewer_id: str = Header(min_length=1)) -> str:
    """Who is approving. A stand-in for real auth, like X-Merchant-Id: it must never come from the request body."""
    return x_reviewer_id


def get_registry(
    pool: AsyncConnectionPool = Depends(get_pool),
    merchant_id: str = Depends(get_merchant_id),
    embedder: Embedder = Depends(get_embedder),
    graph_driver: AsyncDriver | None = Depends(get_graph_driver),
) -> ToolRegistry:
    return build_registry(pool=pool, merchant_id=merchant_id, embedder=embedder, graph_driver=graph_driver)


ASK_LIMIT_PER_MINUTE = 30  # each request is 2 to 3 model calls, so this is the knob that protects the allowance


def get_rate_limiter(redis: Redis | None = Depends(get_redis)) -> RateLimiter | None:
    if redis is None:
        return None
    return RateLimiter(redis, limit=int(os.environ.get("ASK_RATE_LIMIT", ASK_LIMIT_PER_MINUTE)), window_s=60)


async def enforce_ask_limit(
    response: Response,
    merchant_id: str = Depends(get_merchant_id),
    limiter: RateLimiter | None = Depends(get_rate_limiter),
) -> None:
    """Counts each ask against its merchant. Over the limit: 429 with Retry-After, before any model is called."""
    if limiter is None:
        return
    result = await limiter.check("ask", merchant_id)
    if not result.allowed:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please wait a moment and try again.",  # fixed text: nothing internal in it
            headers={"Retry-After": str(result.retry_after_s)},
        )
    if not result.degraded:
        response.headers["X-RateLimit-Limit"] = str(result.limit)
        response.headers["X-RateLimit-Remaining"] = str(result.remaining)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/refund-agent/ask", dependencies=[Depends(enforce_ask_limit)])
async def ask_refund_agent(
    req: AskRequest,
    llm: LLMClient = Depends(get_llm),
    tools: ToolRegistry = Depends(get_registry),
) -> AskResponse:
    started = time.perf_counter()
    result = await run_agent(llm, tools, req.question)
    model_steps = [step for step in result.steps if step.kind == "model"]
    usages = [step.usage for step in model_steps if step.usage]
    costs = [step.usage.cost_usd if step.usage else None for step in model_steps]
    return AskResponse(
        answer=result.final_text,
        stop_reason=result.stop_reason,
        num_steps=len(model_steps),
        steps=result.steps,
        duration_ms=(time.perf_counter() - started) * 1000,
        input_tokens=sum(u.input_tokens for u in usages),
        output_tokens=sum(u.output_tokens for u in usages),
        cost_usd=None if not costs or None in costs else sum(costs),  # a partial sum would understate the cost
    )


# The reviewer screens below need X-Reviewer-Id even to read, like approving does: they are a person's tools, not the
# model's. Where the endpoint does not record who it was, the header is required through `dependencies`.


@app.get("/refunds", dependencies=[Depends(get_reviewer_id)])
async def refunds(
    status: RefundStatus | None = Query(default=None),
    pool: AsyncConnectionPool = Depends(get_pool),
    merchant_id: str = Depends(get_merchant_id),
) -> list[RefundSummary]:
    """This merchant's refund requests, newest first; `status` narrows it to one status."""
    return [RefundSummary(**row) for row in await list_refunds(pool, merchant_id, status)]


@app.get("/refunds/{refund_id}", dependencies=[Depends(get_reviewer_id)])
async def refund_detail(
    refund_id: int = Path(gt=0),
    pool: AsyncConnectionPool = Depends(get_pool),
    merchant_id: str = Depends(get_merchant_id),
) -> RefundDetail:
    """One request with its audit trail: who asked, who approved, when it was paid."""
    row = await get_refund_detail(pool, merchant_id, refund_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No refund request with that id.")
    return RefundDetail(**row)


DEMO_RESETS_PER_MINUTE = 2  # across everyone: a reset rewrites what every visitor sees
_demo_reset_running = asyncio.Lock()


@app.post("/demo/reset")
async def reset_demo_data(
    pool: AsyncConnectionPool = Depends(get_pool),
    graph_driver: AsyncDriver | None = Depends(get_graph_driver),
    provider: PaymentProvider = Depends(get_provider),
    reviewer_id: str = Depends(get_reviewer_id),
    redis: Redis | None = Depends(get_redis),
) -> dict:
    """Puts the demo data back (refund_harness/demo.py): visitors approve and reject everything, and this undoes it."""
    if redis is not None:
        limit = await RateLimiter(redis, limit=DEMO_RESETS_PER_MINUTE, window_s=60).check("demo-reset", "everyone")
        if not limit.allowed:
            raise HTTPException(status_code=429, detail="The demo was just reset. Please wait a moment.", headers={"Retry-After": str(limit.retry_after_s)})
    if _demo_reset_running.locked():
        raise HTTPException(status_code=409, detail="A reset is already running.")
    async with _demo_reset_running:
        try:
            result = await reset_demo(pool, graph_driver, provider)
        except PaymentError:
            raise HTTPException(status_code=503, detail="The payment provider could not be reached, so the demo was not reset.")
    log.info("demo data reset by %s: %d requests, %d unpaid", reviewer_id, result.requests, result.unpaid)
    return {"requests": result.requests, "unpaid": result.unpaid}


@app.get("/activity", dependencies=[Depends(get_reviewer_id)])
async def activity(
    limit: int = Query(default=20, ge=1, le=100),
    pool: AsyncConnectionPool = Depends(get_pool),
    merchant_id: str = Depends(get_merchant_id),
) -> list[ActivityItem]:
    """The latest things that happened to this merchant's refunds: filed, approved, rejected, paid."""
    return [ActivityItem(**row) for row in await list_activity(pool, merchant_id, limit)]


@app.get("/customers/{customer_id}", dependencies=[Depends(get_reviewer_id)])
async def customer_detail(
    customer_id: str = Path(min_length=1, max_length=64),
    pool: AsyncConnectionPool = Depends(get_pool),
    merchant_id: str = Depends(get_merchant_id),
) -> CustomerDetail:
    """A customer's orders and the refund requests on them, for a reviewer weighing a request."""
    row = await get_customer(pool, merchant_id, customer_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No customer with that id.")
    return CustomerDetail(**row)


@app.get("/ledger", dependencies=[Depends(get_reviewer_id)])
async def ledger(
    pool: AsyncConnectionPool = Depends(get_pool),
    provider: PaymentProvider = Depends(get_provider),
    merchant_id: str = Depends(get_merchant_id),
) -> Ledger:
    """Our approved and issued refunds next to the provider's payouts, line by line (payments/reconcile.py)."""
    # Every one of them, not a page: a payout left out here would show up as one the provider made on its own.
    ours = [
        *await list_refunds(pool, merchant_id, status="issued", limit=None),
        *await list_refunds(pool, merchant_id, status="approved", limit=None),
    ]
    try:
        theirs = await provider.list_refunds()
    except PaymentError as error:
        log.warning("ledger: provider unavailable (%s)", error)
        return Ledger(lines=[], available=False)
    lines = sorted(reconcile(merchant_id, ours, theirs), key=lambda line: line.refund_id or 0, reverse=True)
    return Ledger(lines=[LedgerLineOut.model_validate(line, from_attributes=True) for line in lines], available=True)


def _queue_depths(sqs) -> tuple[QueueDepth, QueueDepth]:
    urls = [sqs.get_queue_url(QueueName=name)["QueueUrl"] for name in (PAYOUTS_QUEUE, dead_letter_queue_name(PAYOUTS_QUEUE))]
    main, dead = (QueueDepth(**queue_counts(sqs, url)) for url in urls)
    return main, dead


@app.get("/ops/summary", dependencies=[Depends(get_reviewer_id)])
async def ops_summary(
    pool: AsyncConnectionPool = Depends(get_pool),
    sqs=Depends(get_sqs),
    merchant_id: str = Depends(get_merchant_id),
) -> OpsSummary:
    """Requests and money by status for this merchant, and the payout pipeline's backlog (queues are shared)."""
    counts = await refund_status_counts(pool, merchant_id)
    try:
        payouts, dead = await asyncio.to_thread(_queue_depths, sqs)
    except (BotoCoreError, ClientError) as error:
        log.warning("ops summary: queue unavailable (%s)", type(error).__name__)
        payouts = dead = None
    return OpsSummary(**counts, payouts_queue=payouts, dead_letters=dead)


@app.post("/refunds/{refund_id}/approve")
async def approve_refund_request(
    refund_id: int = Path(gt=0),
    pool: AsyncConnectionPool = Depends(get_pool),
    merchant_id: str = Depends(get_merchant_id),
    reviewer_id: str = Depends(get_reviewer_id),
) -> ApproveResponse:
    """The human approval step. Deliberately NOT a tool: the model must never be able to approve its own requests."""
    async with pool.connection() as conn:
        approved = await approve_refund(conn, merchant_id, refund_id, reviewer_id)
    if not approved:
        # One answer for "no such refund", "another merchant's refund" and "already approved",
        # so nothing reveals that a refund exists for someone else.
        raise HTTPException(status_code=404, detail="No pending refund request with that id.")
    return ApproveResponse(refund_id=refund_id, status="approved")


@app.post("/refunds/{refund_id}/reject")
async def reject_refund_request(
    body: RejectRequest,
    refund_id: int = Path(gt=0),
    pool: AsyncConnectionPool = Depends(get_pool),
    merchant_id: str = Depends(get_merchant_id),
    reviewer_id: str = Depends(get_reviewer_id),
) -> RejectResponse:
    """The human decision the other way, with the reviewer's reason. Like approval, not a tool."""
    async with pool.connection() as conn:
        rejected = await reject_refund(conn, merchant_id, refund_id, reviewer_id, body.reason)
    if not rejected:
        raise HTTPException(status_code=404, detail="No pending refund request with that id.")
    return RejectResponse(refund_id=refund_id, status="rejected")


# Error translation. Messages are fixed on purpose: exception text may contain internals
# (e.g. CLI stderr) and must never reach the client.
def _error(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": message})


@app.exception_handler(ReplyParseError)
async def on_bad_reply(request, exc):
    return _error(502, "The assistant's reply was unusable. Please try again.")


@app.exception_handler(ModelCallError)
async def on_model_error(request, exc):
    return _error(502, "The assistant is unavailable. Please try again.")


@app.exception_handler(ModelTimeoutError)
async def on_model_timeout(request, exc):
    return _error(504, "The assistant took too long. Please try again.")
