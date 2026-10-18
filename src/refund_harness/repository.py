import asyncio

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool


async def get_order_row(pool: AsyncConnectionPool, merchant_id: str, order_id: str) -> dict | None:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT item, status, total_cents, delivered_at FROM orders WHERE merchant_id = %s AND id = %s",
            (merchant_id, order_id)
        )
        row = await cur.fetchone()
        return row


async def request_refund(conn: AsyncConnection, merchant_id: str, order_id: str, amount_cents: int) -> int | None:
    async with conn:
        cur = await conn.execute(
            "INSERT INTO refund_requests (merchant_id, order_id, amount_cents) "
            "VALUES  (%s, %s, %s)" \
            "ON CONFLICT (merchant_id, order_id) WHERE status <> 'rejected' DO NOTHING RETURNING id",
            (merchant_id, order_id, amount_cents),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return row["id"]