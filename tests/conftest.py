import httpx
import pytest
from helpers import SANDBOX, SANDBOX_CHUNKS, Refunds
from pydantic import BaseModel

from refund_harness.app import app, get_embedder, get_pool
from refund_harness.db import create_pool
from refund_harness.embedder import FakeEmbedder
from refund_harness.tools import Tool, ToolRegistry

ORDERS = {"A100": "delivered 3 days ago"}


class OrderArgs(BaseModel):
    order_id: str


async def _get_order(args: OrderArgs) -> str:
    return ORDERS[args.order_id]  # KeyError for unknown orders, on purpose


def make_registry() -> ToolRegistry:
    return ToolRegistry(
        [Tool("get_order", "Look up an order's status by id.", OrderArgs, _get_order)]
    )

@pytest.fixture
def registry():
    return make_registry()

@pytest.fixture
async def pool():
    pool = await create_pool()
    yield pool
    await pool.close()


@pytest.fixture
async def refunds(pool):
    """Starts the test with empty refund tables (leftovers would change outcomes) and lets it inspect them."""
    async with pool.connection() as conn:
        await conn.execute("TRUNCATE TABLE refund_requests, refund_events")
    return Refunds(pool)


@pytest.fixture
async def client(pool):
    app.dependency_overrides[get_pool] = lambda: pool
    app.dependency_overrides[get_embedder] = lambda: FakeEmbedder()  # tests never call Google
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(headers={"X-Merchant-Id": "acme"}, transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()      # runs after each test, so fakes can't leak


@pytest.fixture
async def sandbox(pool):
    """A merchant with a few policy chunks that have no vectors yet; removed again afterwards."""

    async def wipe():
        async with pool.connection() as conn:
            await conn.execute("DELETE FROM policy_chunks WHERE merchant_id = %s", (SANDBOX,))
            await conn.execute("DELETE FROM merchants WHERE id = %s", (SANDBOX,))

    await wipe()  # in case an earlier run crashed halfway
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO merchants (id, name) VALUES (%s, 'Ingest sandbox')", (SANDBOX,))
        for title, body in SANDBOX_CHUNKS:
            await conn.execute(
                "INSERT INTO policy_chunks (merchant_id, title, body) VALUES (%s, %s, %s)", (SANDBOX, title, body)
            )
    yield SANDBOX
    await wipe()
