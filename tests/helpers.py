"""Shared test helpers: named constants, a fake clock, and small wrappers around the refund tables.

Seed data these tests rely on (db/init/02_seed.sql):
  acme   A100  headphones  $59.00  delivered a few days before the DB was created
  acme   A300  keyboard    $89.00  shipped, not delivered
  globex A100  blender    $120.00  (same order id as acme's, different merchant on purpose)
"""

import json
from datetime import datetime, timedelta
from typing import Callable

import sentry_sdk
from psycopg_pool import AsyncConnectionPool
from sentry_sdk.transport import Transport

from refund_harness.data.repository import (
    approve_refund,
    get_order_row,
    request_refund,
    submit_refund,
)

ACME = "acme"
GLOBEX = "globex"
A100_TOTAL_CENTS = 5900
DAY = timedelta(days=1)

# A throwaway merchant for tests that write policy chunks or embeddings. It never touches the seed merchants,
# whose embeddings come from the real Gemini model and cost money to recreate.
SANDBOX = "ingest-sandbox"
SANDBOX_CHUNKS = [
    ("Return window", "Items may be returned within 30 days."),
    ("Gift cards", "Gift cards are final sale."),
    ("Cancellations", "Unshipped orders can be cancelled."),
    ("Secret", "zebra unicorn rainbow"),  # words no real merchant uses: proves nothing leaks across merchants
]


async def moment_after_delivery(pool: AsyncConnectionPool, after: timedelta) -> datetime:
    """A fixed instant `after` acme's A100 was delivered.

    Seed dates are relative to when the DB was created, so tests must not use the real clock:
    "3 days after delivery" is eligible today and would be 40 days stale next month.
    """
    row = await get_order_row(pool, ACME, "A100")
    return row["delivered_at"] + after


def fixed_clock(moment: datetime) -> Callable[[], datetime]:
    return lambda: moment


# Each helper below borrows its own connection from the pool, like a real request would.

async def request(pool: AsyncConnectionPool, merchant_id: str, order_id: str, amount_cents: int) -> int | None:
    async with pool.connection() as conn:
        return await request_refund(conn, merchant_id, order_id, amount_cents)


async def approve(pool: AsyncConnectionPool, refund_id: int, actor: str | None = "reviewer-1", merchant_id: str = ACME) -> bool:
    async with pool.connection() as conn:
        return await approve_refund(conn, merchant_id, refund_id, actor)


async def submit(pool: AsyncConnectionPool, merchant_id: str, order_id: str, amount_cents: int, now: datetime):
    async with pool.connection() as conn:
        return await submit_refund(conn, merchant_id, order_id, amount_cents, now)


class Refunds:
    """Looks at what is really stored in the refund tables, so tests check the database and not just return values."""

    def __init__(self, pool: AsyncConnectionPool):
        self._pool = pool

    async def _fetch(self, sql: str, params: tuple = ()) -> list[dict]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return await cur.fetchall()

    async def count(self) -> int:
        return (await self._fetch("SELECT count(*) AS n FROM refund_requests"))[0]["n"]

    async def rows(self) -> list[dict]:
        return await self._fetch("SELECT merchant_id, order_id, amount_cents, status FROM refund_requests")

    async def status_of(self, refund_id: int) -> str:
        rows = await self._fetch("SELECT status FROM refund_requests WHERE id = %s", (refund_id,))
        return rows[0]["status"]

    async def events_for(self, refund_id: int) -> list[dict]:
        return await self._fetch("SELECT event, actor FROM refund_events WHERE refund_id = %s", (refund_id,))

    async def set_status(self, refund_id: int, status: str) -> None:
        """Test setup only: put a refund into a state, without going through the code under test."""
        await self._fetch_nothing("UPDATE refund_requests SET status = %s WHERE id = %s", (status, refund_id))

    async def _fetch_nothing(self, sql: str, params: tuple) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(sql, params)


FAKE_DSN = "https://publickey@o0.ingest.sentry.io/0"


class CatchingTransport(Transport):
    """Stands in for Sentry's network connection: collects what would have been sent, so nothing leaves the machine."""

    def __init__(self):
        super().__init__()
        self.envelopes = []

    def capture_envelope(self, envelope):
        self.envelopes.append(envelope)

    @property
    def events(self) -> list[dict]:
        """Every error event Sentry would have sent, as plain dicts."""
        payloads = [item.payload.json for e in self.envelopes for item in e.items]
        return [p for p in payloads if p and ("exception" in p or "message" in p)]

    @property
    def reports(self) -> str:
        """Everything that would have been sent, as one searchable string."""
        return "\n".join(json.dumps(item.payload.json, default=str) for e in self.envelopes for item in e.items if item.payload.json)


def reset_sentry() -> None:
    """Put Sentry back to 'off' after a test, without switching on any of its default integrations.

    sentry_sdk.init(dsn=None) would auto-enable them (google_genai among them), and that would change what the next
    test sees. Note that is_active() is True for ANY real client, DSN or not, so it can't tell on from off.
    """
    sentry_sdk.init(dsn=None, default_integrations=False, auto_enabling_integrations=False)
