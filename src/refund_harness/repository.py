import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool


class RefundOutcome(StrEnum):
    CREATED = "created"
    DUPLICATE = "duplicate"
    INELIGIBLE = "ineligible"
    ORDER_NOT_FOUND = "order_not_found"


@dataclass(frozen=True)
class SubmitRefundResult:
    """What happened. The tool layer decides how to word this for the model."""

    outcome: RefundOutcome
    refund_id: int | None = None  # set only when CREATED
    reasons: tuple[str, ...] = ()  # set only when INELIGIBLE


def to_vector_literal(vector: list[float]) -> str:
    """pgvector's text form, e.g. '[0.1,0.2]'. Send it with a `%s::vector` cast so Postgres converts it."""
    return "[" + ",".join(str(x) for x in vector) + "]"


async def get_order_row(pool: AsyncConnectionPool, merchant_id: str, order_id: str) -> dict | None:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT item, status, total_cents, delivered_at FROM orders WHERE merchant_id = %s AND id = %s",
            (merchant_id, order_id)
        )
        row = await cur.fetchone()
        return row


async def get_order_refund_id(conn: AsyncConnection, merchant_id: str, order_id: str) -> str:
    cur = await conn.execute(
        "SELECT id FROM refund_requests WHERE merchant_id = %s AND order_id = %s",
        (merchant_id, order_id)
    )
    row = await cur.fetchone()
    return row["id"]


async def request_refund(conn: AsyncConnection, merchant_id: str, order_id: str, amount_cents: int) -> int | None:
    """Insert a refund request; None if a live one already exists. Does not manage the transaction: the caller does."""
    cur = await conn.execute(
        "INSERT INTO refund_requests (merchant_id, order_id, amount_cents) "
        "VALUES  (%s, %s, %s) "
        "ON CONFLICT (merchant_id, order_id) WHERE status <> 'rejected' DO NOTHING RETURNING id",
        (merchant_id, order_id, amount_cents),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return row["id"]


async def approve_refund(conn: AsyncConnection, merchant_id: str, refund_id: int, actor: str) -> bool:
    async with conn.transaction():
        cur = await conn.execute(
            "UPDATE refund_requests SET status = 'approved' "
            "WHERE id = %s AND merchant_id = %s "
            "AND status = 'pending' RETURNING id",
            (refund_id, merchant_id),
        )
        row = await cur.fetchone()
        if row is None:
            return False
        cur = await conn.execute(
            "INSERT INTO refund_events (refund_id, event, actor) "
            "VALUES (%s, %s, %s)",
            (refund_id, 'approved', actor)
        )
        return True


def check_refund_eligibility(order_row, amount_cents, now: datetime) -> list[str]:
    order_date = order_row["delivered_at"]
    order_amount = order_row["total_cents"]
    order_status = order_row["status"]
    rejections = []

    if amount_cents > order_amount:
        rejections.append("Cannot refund more than order amount.")
    if order_status != "delivered":
        rejections.append("Invalid order, has not been delivered.")

    if order_date is None:
        rejections.append("Order date not found.")
    else:
        if (now - order_date) >= timedelta(days=31):
            rejections.append("It has been over 30 days since delivery, order ineligible for refund.")
        if (now - order_date) < timedelta(days=0):
            rejections.append("Invalid order date.")
    
    return rejections


async def submit_refund(
    conn: AsyncConnection, merchant_id: str, order_id: str, amount_cents: int, now: datetime
) -> SubmitRefundResult:
    async with conn.transaction():
        cur = await conn.execute(
            "SELECT item, status, total_cents, delivered_at "
            "FROM orders "
            "WHERE merchant_id = %s AND id = %s "
            "FOR UPDATE",
            (merchant_id, order_id)
        )
        order_row = await cur.fetchone()
        if order_row is None:
            return SubmitRefundResult(RefundOutcome.ORDER_NOT_FOUND)
        rejections = check_refund_eligibility(order_row=order_row, amount_cents=amount_cents, now=now)
        if rejections:
            return SubmitRefundResult(RefundOutcome.INELIGIBLE, reasons=tuple(rejections))

        order_refund_id = await request_refund(conn=conn, merchant_id=merchant_id, order_id=order_id, amount_cents=amount_cents)
        if order_refund_id is None:
            return SubmitRefundResult(RefundOutcome.DUPLICATE)
        return SubmitRefundResult(RefundOutcome.CREATED, refund_id=order_refund_id)


async def search_policy_keyword(pool: AsyncConnectionPool, merchant_id, query, limit=3) -> list[dict]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT title, body, ts_rank(tsv, query) AS rank " \
            "FROM policy_chunks, websearch_to_tsquery('english', %s) AS query " \
            "WHERE merchant_id = %s AND tsv @@ query " \
            "ORDER BY rank DESC " \
            "LIMIT %s",
            (query, merchant_id, limit)
        )
        result = await cur.fetchall()
        return result


async def search_policy_by_vector(
    pool: AsyncConnectionPool, merchant_id: str, query_vector: list[float], limit: int = 3
) -> list[dict]:
    """The merchant's policy chunks nearest in meaning to `query_vector`, closest first.

    `distance` is cosine distance: 0 means the same direction, larger means less related (it can reach 2).
    It always returns the nearest `limit` chunks, however unrelated: judging relevance is the caller's job.
    """
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT title, body, embedding <=> %s::vector AS distance "
            "FROM policy_chunks "
            "WHERE merchant_id = %s AND embedding IS NOT NULL "
            "ORDER BY distance "
            "LIMIT %s",
            (to_vector_literal(query_vector), merchant_id, limit),
        )
        return await cur.fetchall()
