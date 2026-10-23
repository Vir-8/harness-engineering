"""The refund agent's tools: order lookup, policy search and refund requests. The merchant is bound here, when the
tools are built, never a tool argument the model could change."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import assert_never

from neo4j import AsyncDriver
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

from refund_harness.agent.tools import Tool, ToolRegistry
from refund_harness.data.repository import (
    RefundOutcome,
    SubmitRefundResult,
    get_order_row,
    search_policy_by_vector,
    submit_refund,
)
from refund_harness.graph.risk import attach_risk_flags
from refund_harness.retrieval.embedder import Embedder

# Policy chunks farther than this (cosine distance) from the question are treated as "not about this".
# Measured on gemini-embedding-2 (768 dims): on-topic best matches were 0.20-0.39, unrelated questions' best matches
# 0.42-0.45. The gap is thin (8 questions): re-measure whenever the embedding model, its dimensions or the policy text
# change.
MAX_POLICY_DISTANCE = 0.41
POLICY_CHUNKS_SHOWN = 3


class OrderArgs(BaseModel):
    order_id: str


class RefundArgs(BaseModel):
    order_id: str
    amount_cents: int = Field(gt=0, description="Refund amount in CENTS (5900 means $59.00), not dollars.")


class SearchPolicyArgs(BaseModel):
    question: str = Field(min_length=2, max_length=500, description="What the customer wants to know, in plain words.")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def build_registry(
    pool: AsyncConnectionPool,
    merchant_id: str,
    embedder: Embedder,
    now: Callable[[], datetime] = _utc_now,
    graph_driver: AsyncDriver | None = None,
) -> ToolRegistry:
    """`now` is the clock: the real one in production, a fixed one in tests.

    `graph_driver`, when given, lets a new refund request be checked for linked accounts (graph/risk.py). That check is
    advisory and only for the human approver: its result is stored on the request and NEVER put in the tool's reply.
    """

    async def get_order(args: OrderArgs) -> str:
        row = await get_order_row(pool, merchant_id, args.order_id)
        if row is None:
            return order_not_found_text(args.order_id)
        return format_order(args.order_id, row, now())

    async def search_policy(args: SearchPolicyArgs) -> str:
        question_vector = await embedder.embed_query(args.question)
        chunks = await search_policy_by_vector(pool, merchant_id, question_vector, POLICY_CHUNKS_SHOWN)
        return format_policy_results(chunks)

    async def submit_order_refund(args: RefundArgs) -> str:
        async with pool.connection() as conn:
            result = await submit_refund(conn, merchant_id, args.order_id, args.amount_cents, now())
        if result.outcome == RefundOutcome.CREATED and graph_driver is not None:
            # after the transaction has committed, so the order lock is not held while Neo4j is asked; never raises
            await attach_risk_flags(pool, graph_driver, merchant_id, args.order_id, result.refund_id)
        return format_refund_result(args.order_id, args.amount_cents, result)

    return ToolRegistry([
        Tool("get_order", "Look up an order by id.", OrderArgs, get_order),
        Tool(
            "search_policy",
            "Search this merchant's refund and return policy for the passages that answer a customer's question. "
            "Use it before stating any policy; never guess a policy from memory.",
            SearchPolicyArgs,
            search_policy,
        ),
        Tool(
            "submit_order_refund",
            "Submit a refund request for an order. This only REQUESTS the refund: it stays pending until a human approves it. "
            "Any amount up to the order total can be requested, including part of it. Use the amount the customer asked "
            "for; if it is not possible, explain why and ask, rather than submitting a different amount. Do not call it "
            "when the order or the policy rules a refund out (for example the order has not arrived, or the return "
            "window has passed): tell the customer why instead, even if they ask you to submit anyway.",
            RefundArgs,
            submit_order_refund,
        ),
    ])


def format_policy_results(chunks: list[dict], max_distance: float = MAX_POLICY_DISTANCE) -> str:
    """What the model reads after a policy search. `chunks` come closest-first, each with a `distance`."""
    relevant = [c for c in chunks if c["distance"] <= max_distance]
    if not relevant:
        return (
            "No policy text clearly matches that question. Do not guess the policy: "
            "tell the customer you cannot confirm it, or try searching again with different words."
        )
    return "\n\n".join(f"{c['title']}\n{c['body']}" for c in relevant)


def order_not_found_text(order_id: str) -> str:
    """One wording for 'no such order', whether it never existed or belongs to another merchant."""
    return f"No order could be found with id: {order_id}."


def format_refund_result(order_id: str, amount_cents: int, result: SubmitRefundResult) -> str:
    match result.outcome:
        case RefundOutcome.CREATED:
            return (
                f"Refund request {result.refund_id} for order {order_id} (${_dollars(amount_cents)}) was submitted "
                "and is pending human approval. No money has been refunded yet."
            )
        case RefundOutcome.DUPLICATE:
            return _existing_request_text(order_id, result)
        case RefundOutcome.INELIGIBLE:
            reasons = "\n".join(f"- {r}" for r in result.reasons)
            return f"Order {order_id} is not eligible for this refund:\n{reasons}"
        case RefundOutcome.ORDER_NOT_FOUND:
            return order_not_found_text(order_id)
        case _:
            assert_never(result.outcome)


def _existing_request_text(order_id: str, result: SubmitRefundResult) -> str:
    # Says where the existing request stands: told only "it exists", the model guessed, and told a customer whose
    # refund had been PAID that it was still waiting for review. The model echoes this text, so a state that is not paid
    # never says the money was sent.
    request = f"A refund request ({result.refund_id}) for order {order_id}" if result.refund_id else f"A refund request for order {order_id}"
    match result.existing_status:
        case "pending":
            state = "already exists and is waiting for a human to review it. No money has been refunded yet."
        case "approved":
            state = "already exists. A person approved it and the payout is in progress."
        case "issued":
            amount = f" (${_dollars(result.existing_amount_cents)})" if result.existing_amount_cents is not None else ""
            state = f"already exists and was paid out in full{amount}; the money went back to the customer."
        case _:
            state = "already exists."
    return f"{request} {state} No new request was created."


def _dollars(cents: int) -> str:
    dollars, rest = divmod(cents, 100)
    return f"{dollars}.{rest:02d}"


def format_order(order_id: str, row: dict, now: datetime) -> str:
    delivered_at = row["delivered_at"]
    if delivered_at is None:
        delivery = "not delivered yet"
    else:
        days = (now - delivered_at).days
        delivery = f"{days} day{'' if days == 1 else 's'} ago ({delivered_at.date().isoformat()})"
    return (
        f"Order {order_id}: {row['item']}\n"
        f"Status: {row['status']}\n"
        f"Total: ${_dollars(row['total_cents'])}\n"
        f"Delivered: {delivery}"
    )
