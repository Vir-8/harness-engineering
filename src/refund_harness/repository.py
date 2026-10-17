from psycopg_pool import AsyncConnectionPool


async def get_order_row(pool: AsyncConnectionPool, merchant_id: str, order_id: str) -> dict | None:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT item, status, total_cents, delivered_at FROM orders WHERE merchant_id = %s AND id = %s",
            (merchant_id, order_id)
        )
        row = await cur.fetchone()
        return row