"""HTTP layer. The agent logic lives elsewhere; this file only translates HTTP <-> harness."""

import os
from contextlib import asynccontextmanager
from functools import lru_cache

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Request, Response
from fastapi.responses import JSONResponse
from neo4j import AsyncDriver
from psycopg_pool import AsyncConnectionPool
from redis.asyncio import Redis

from refund_harness.adapters.claude_cli import DEFAULT_MODEL, ClaudeCLIClient
from refund_harness.adapters.gemini_embeddings import GeminiEmbedder
from refund_harness.agent.errors import (
    ModelCallError,
    ModelTimeoutError,
    ReplyParseError,
)
from refund_harness.agent.llm import LLMClient
from refund_harness.agent.loop import MAX_STEPS, run_agent
from refund_harness.agent.tools import ToolRegistry
from refund_harness.cache.client import create_redis
from refund_harness.cache.embedding_cache import CachedEmbedder
from refund_harness.cache.rate_limit import RateLimiter
from refund_harness.data.db import create_pool
from refund_harness.data.repository import approve_refund, list_pending_refunds
from refund_harness.graph.driver import create_graph_driver
from refund_harness.observability import enable_tracing, init_sentry
from refund_harness.refund_tools import build_registry
from refund_harness.retrieval.embedder import Embedder
from refund_harness.schemas import (
    ApproveResponse,
    AskRequest,
    AskResponse,
    PendingRefund,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await create_pool()
    app.state.graph_driver = create_graph_driver()  # creating a driver does not connect: Neo4j being down cannot stop startup
    app.state.redis = create_redis()  # same for Redis: it connects on first use, and every use tolerates it being gone
    yield
    await app.state.redis.aclose()
    await app.state.graph_driver.close()
    await app.state.pool.close()

init_sentry()  # before the app exists: Sentry hooks into the framework as it starts. Off unless SENTRY_DSN is set.
app = FastAPI(title="Refund agent", lifespan=lifespan)
enable_tracing(app)  # prints each request as a tree of spans; TRACING=off disables

# Dependencies: endpoints ask for these with Depends(...), so tests can swap them out.
def get_llm() -> LLMClient:
    return ClaudeCLIClient(model=os.environ.get("CLAUDE_MODEL", DEFAULT_MODEL))


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
    result = await run_agent(llm, tools, req.question, max_steps=MAX_STEPS)
    return AskResponse(
        answer=result.final_text,
        stop_reason=result.stop_reason,
        num_steps=sum(m.role == "assistant" for m in result.messages),
    )


@app.get("/refunds/pending")
async def pending_refunds(
    pool: AsyncConnectionPool = Depends(get_pool),
    merchant_id: str = Depends(get_merchant_id),
    reviewer_id: str = Depends(get_reviewer_id),  # required like on approval: this is a reviewer's screen, not the model's
) -> list[PendingRefund]:
    """What a human approver reads before approving: each pending request with its linked-accounts notes."""
    return [PendingRefund(**row) for row in await list_pending_refunds(pool, merchant_id)]


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
