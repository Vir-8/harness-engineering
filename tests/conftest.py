import os

# Tests must never report to the real Sentry project, even when run with `--env-file .env`.
# This has to happen before refund_harness.app is imported, because importing it starts Sentry.
os.environ.pop("SENTRY_DSN", None)
# Nor should every test request print a trace tree: tracing is on by default, so switch it off before the import.
os.environ["TRACING"] = "off"

import httpx  # noqa: E402
import pytest  # noqa: E402
from graph_helpers import A, B, World, seed_ring, wipe  # noqa: E402
from helpers import (  # noqa: E402
    FAKE_DSN,
    SANDBOX,
    SANDBOX_CHUNKS,
    CatchingTransport,
    Refunds,
    reset_sentry,
)
from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from refund_harness.agent.tools import Tool, ToolRegistry  # noqa: E402
from refund_harness.app import app, get_embedder, get_graph_driver, get_pool  # noqa: E402
from refund_harness.data.db import create_pool  # noqa: E402
from refund_harness.graph.driver import create_graph_driver  # noqa: E402
from refund_harness.observability import init_sentry  # noqa: E402
from refund_harness.retrieval.embedder import FakeEmbedder  # noqa: E402

# Our own spans (the agent loop) use the process-wide tracer provider, which can be set only once per process.
# Tests set it here, to an in-memory exporter, and read what a test produced through the `spans` fixture.
_SPAN_EXPORTER = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(_SPAN_EXPORTER))
trace.set_tracer_provider(_provider)

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
    app.dependency_overrides[get_graph_driver] = lambda: None  # ...and the API tests need no Neo4j
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


@pytest.fixture
def spans():
    """The spans finished during this test, in the order they started."""
    _SPAN_EXPORTER.clear()
    yield lambda: sorted(_SPAN_EXPORTER.get_finished_spans(), key=lambda s: s.start_time)
    _SPAN_EXPORTER.clear()


@pytest.fixture
def sentry_events():
    """Turns Sentry on for one test, sending into memory instead of the network. Read `.events` afterwards."""
    transport = CatchingTransport()
    init_sentry(FAKE_DSN, export_traces=False, transport=transport)  # export_traces=False: no OTLP exporter on the shared provider
    yield transport
    reset_sentry()


@pytest.fixture
async def world(pool):
    """Two throwaway merchants (graph-a, graph-b) with the same ring of customers, in Postgres; the graph side is empty."""
    driver = create_graph_driver()
    await wipe(pool, driver)  # in case an earlier run crashed halfway
    await seed_ring(pool, A)
    await seed_ring(pool, B)
    yield World(pool, driver)
    await wipe(pool, driver)
    await driver.close()
