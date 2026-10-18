import asyncio

from psycopg import AsyncConnection
from psycopg.errors import NotNullViolation
from psycopg_pool import AsyncConnectionPool
import pytest

from refund_harness.refund_tools import build_registry
from refund_harness.repository import get_order_row, request_refund, approve_refund


@pytest.fixture
async def clean_refunds(pool: AsyncConnectionPool):
    async with pool.connection() as conn:
        await conn.execute(
                    "TRUNCATE TABLE refund_requests, refund_events",
        )


async def check_refund_request_status(pool: AsyncConnectionPool, order_id, merchant_id):
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT status FROM refund_requests WHERE order_id = %s AND merchant_id = %s",
            (order_id, merchant_id)
        )
        row = await cur.fetchone()
        return row["status"]


async def check_refund_events(pool: AsyncConnectionPool, refund_id):
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id FROM refund_events WHERE refund_id = %s",
            (refund_id,)
        )
        row = await cur.fetchone()
        return row


async def test_returns_the_merchants_own_order(pool: AsyncConnectionPool):
    row = await get_order_row(pool, "acme", "A100")
    assert row["item"] == "headphones"
    assert row["total_cents"] == 5900


async def test_missing_order_returns_none(pool: AsyncConnectionPool):
    row = await get_order_row(pool, '', '')
    assert row == None


async def test_other_merchants_order_is_invisible(pool: AsyncConnectionPool):
    row = await get_order_row(pool, "globex", "A300")
    assert row is None          # the order exists in the table, but not for this merchant


async def test_harness_gets_order(pool: AsyncConnectionPool):
    registry = build_registry(pool, "acme")
    result = await registry.get("get_order").run({"order_id": "A100"})
    assert "headphones" in result


@pytest.mark.usefixtures("clean_refunds")
async def test_refund_same_order_twice(pool: AsyncConnectionPool):
    async def attempt():
        async with pool.connection() as conn:
            return await request_refund(conn, "acme", "A100", 5900)

    results = await asyncio.gather(attempt(), attempt())
    assert results.count(None) == 1
    assert sum(isinstance(x, int) for x in results) == 1


@pytest.mark.usefixtures("clean_refunds")
async def test_refund_approves_only_once(pool: AsyncConnectionPool):
    async def attempt(refund_id, merchant_id="acme", actor="user"):
        async with pool.connection() as conn:
            return await approve_refund(conn=conn, merchant_id=merchant_id, refund_id=refund_id, actor=actor)

    async with pool.connection() as conn:
        refund_id = await request_refund(conn, "acme", "A100", 5900)

    results = await asyncio.gather(attempt(refund_id=refund_id), attempt(refund_id=refund_id))

    assert sum(x is True for x in results) == 1
    assert sum(x is False for x in results) == 1


@pytest.mark.usefixtures("clean_refunds")
async def test_refund_failure_stays_pending(pool: AsyncConnectionPool):
    async def attempt(refund_id, merchant_id="acme", actor=None):
        async with pool.connection() as conn:
            return await approve_refund(conn=conn, merchant_id=merchant_id, refund_id=refund_id, actor=actor)

    async with pool.connection() as conn:
        refund_id = await request_refund(conn, "acme", "A100", 5900)

    with pytest.raises(NotNullViolation):
        await attempt(refund_id=refund_id)
    assert await check_refund_request_status(pool, order_id="A100", merchant_id="acme") == "pending"
    assert await check_refund_events(pool, refund_id) is None
