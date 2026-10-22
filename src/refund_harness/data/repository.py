"""Every SQL query the harness runs. Each one is scoped by merchant_id; functions that take a connection leave the
transaction to their caller, and the ones that change a refund's status run as one transaction of their own.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

PAYOUT_TOPIC = "refund-payout"  # outbox topic for "pay out this approved refund"
REFUND_WINDOW_DAYS = 30  # counted in whole days since delivery: 30 days and 23 hours is still 30


class RefundOutcome(StrEnum):
    CREATED = "created"
    DUPLICATE = "duplicate"
    INELIGIBLE = "ineligible"
    ORDER_NOT_FOUND = "order_not_found"


@dataclass(frozen=True)
class SubmitRefundResult:
    """What happened. The tool layer decides how to word this for the model."""

    outcome: RefundOutcome
    refund_id: int | None = None  # CREATED: the new request; DUPLICATE: the request that already exists
    reasons: tuple[str, ...] = ()  # set only when INELIGIBLE
    # DUPLICATE only: where the existing request stands, so the agent can tell the customer the truth instead of guessing
    existing_status: str | None = None
    existing_amount_cents: int | None = None


# --- orders and refund requests ---


async def get_order_row(pool: AsyncConnectionPool, merchant_id: str, order_id: str) -> dict | None:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT item, status, total_cents, delivered_at FROM orders WHERE merchant_id = %s AND id = %s",
            (merchant_id, order_id),
        )
        return await cur.fetchone()


def check_refund_eligibility(order_row: dict, amount_cents: int, now: datetime) -> list[str]:
    """Why this refund cannot be made, one reason per rule broken; an empty list means it is eligible."""
    delivered_at = order_row["delivered_at"]
    reasons = []
    if amount_cents > order_row["total_cents"]:
        reasons.append("Cannot refund more than order amount.")
    if order_row["status"] != "delivered":
        reasons.append("Invalid order, has not been delivered.")
    if delivered_at is None:
        if order_row["status"] == "delivered":  # an order on its way has no delivery date yet: "not delivered" says why
            reasons.append("Delivery date not found.")
    elif delivered_at > now:
        reasons.append("Invalid order date.")
    elif (now - delivered_at).days > REFUND_WINDOW_DAYS:
        reasons.append(f"It has been over {REFUND_WINDOW_DAYS} days since delivery, order ineligible for refund.")
    return reasons


async def insert_refund_request(conn: AsyncConnection, merchant_id: str, order_id: str, amount_cents: int) -> int | None:
    """Insert a pending refund request; None if the order already has a live one (the unique index decides)."""
    cur = await conn.execute(
        """
        INSERT INTO refund_requests (merchant_id, order_id, amount_cents)
        VALUES (%s, %s, %s)
        ON CONFLICT (merchant_id, order_id) WHERE status <> 'rejected' DO NOTHING
        RETURNING id
        """,
        (merchant_id, order_id, amount_cents),
    )
    row = await cur.fetchone()
    return row["id"] if row else None


async def submit_refund(
    conn: AsyncConnection, merchant_id: str, order_id: str, amount_cents: int, now: datetime
) -> SubmitRefundResult:
    """What the agent's submit tool does: check eligibility and file the request, in one transaction.

    The order row stays locked (FOR UPDATE) until the transaction ends, so two requests for the same order are checked
    one after the other, never side by side.
    """
    async with conn.transaction():
        cur = await conn.execute(
            """
            SELECT item, status, total_cents, delivered_at
            FROM orders
            WHERE merchant_id = %s AND id = %s
            FOR UPDATE
            """,
            (merchant_id, order_id),
        )
        order_row = await cur.fetchone()
        if order_row is None:
            return SubmitRefundResult(RefundOutcome.ORDER_NOT_FOUND)
        reasons = check_refund_eligibility(order_row, amount_cents, now)
        if reasons:
            return SubmitRefundResult(RefundOutcome.INELIGIBLE, reasons=tuple(reasons))

        refund_id = await insert_refund_request(conn, merchant_id, order_id, amount_cents)
        if refund_id is None:
            # Still inside the transaction that locked the order row, so this is the request that blocked ours.
            cur = await conn.execute(
                """
                SELECT id, status, amount_cents
                FROM refund_requests
                WHERE merchant_id = %s AND order_id = %s AND status <> 'rejected'
                """,
                (merchant_id, order_id),
            )
            existing = await cur.fetchone()
            return SubmitRefundResult(
                RefundOutcome.DUPLICATE,
                refund_id=existing["id"] if existing else None,
                existing_status=existing["status"] if existing else None,
                existing_amount_cents=existing["amount_cents"] if existing else None,
            )
        return SubmitRefundResult(RefundOutcome.CREATED, refund_id=refund_id)


async def approve_refund(conn: AsyncConnection, merchant_id: str, refund_id: int, actor: str) -> bool:
    """pending -> approved, with an audit event and the payout job, all or nothing. False if it is not this merchant's
    pending request."""
    async with conn.transaction():
        cur = await conn.execute(
            """
            UPDATE refund_requests SET status = 'approved'
            WHERE id = %s AND merchant_id = %s AND status = 'pending'
            RETURNING id
            """,
            (refund_id, merchant_id),
        )
        if await cur.fetchone() is None:
            return False
        await conn.execute(
            "INSERT INTO refund_events (refund_id, event, actor) VALUES (%s, 'approved', %s)", (refund_id, actor)
        )
        # The payout job, in the same transaction: no approval without its job, no job without its approval.
        await add_to_outbox(conn, PAYOUT_TOPIC, {"merchant_id": merchant_id, "refund_id": refund_id})
        return True


async def reject_refund(conn: AsyncConnection, merchant_id: str, refund_id: int, actor: str, reason: str) -> bool:
    """pending -> rejected, with an audit event carrying the reviewer's reason, all or nothing. False if it is not this
    merchant's pending request.

    A rejected request no longer counts as the order's live request, so a new one can be filed for the same order.
    """
    async with conn.transaction():
        cur = await conn.execute(
            """
            UPDATE refund_requests SET status = 'rejected'
            WHERE id = %s AND merchant_id = %s AND status = 'pending'
            RETURNING id
            """,
            (refund_id, merchant_id),
        )
        if await cur.fetchone() is None:
            return False
        await conn.execute(
            "INSERT INTO refund_events (refund_id, event, actor, note) VALUES (%s, 'rejected', %s, %s)",
            (refund_id, actor, reason),
        )
        return True


# --- payouts ---


async def get_approved_refund(pool: AsyncConnectionPool, merchant_id: str, refund_id: int) -> dict | None:
    """The refund if it is approved and belongs to this merchant (ready to be paid out), else None."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT id, order_id, amount_cents
            FROM refund_requests
            WHERE id = %s AND merchant_id = %s AND status = 'approved'
            """,
            (refund_id, merchant_id),
        )
        return await cur.fetchone()


async def mark_refund_issued(conn: AsyncConnection, merchant_id: str, refund_id: int, provider_refund_id: str, actor: str) -> bool:
    """approved -> issued, with the provider's id and an audit event, all or nothing. False if it was not approved."""
    async with conn.transaction():
        cur = await conn.execute(
            """
            UPDATE refund_requests SET status = 'issued', provider_refund_id = %s
            WHERE id = %s AND merchant_id = %s AND status = 'approved'
            RETURNING id
            """,
            (provider_refund_id, refund_id, merchant_id),
        )
        if await cur.fetchone() is None:
            return False
        await conn.execute(
            "INSERT INTO refund_events (refund_id, event, actor) VALUES (%s, 'issued', %s)", (refund_id, actor)
        )
        return True


# --- the outbox: messages that commit with the change they announce ---


async def add_to_outbox(conn: AsyncConnection, topic: str, payload: dict) -> None:
    """Queue a message for the relay to send. Call it inside the transaction whose change the message announces."""
    await conn.execute("INSERT INTO outbox (topic, payload) VALUES (%s, %s)", (topic, Jsonb(payload)))


async def claim_unsent_outbox(conn: AsyncConnection, topic: str, limit: int) -> list[dict]:
    """The oldest unsent messages of a topic, LOCKED until the caller's transaction ends.

    SKIP LOCKED: a second relay running at the same time skips the rows this one holds instead of waiting for them,
    so relays can run side by side without sending the same row at the same moment.
    """
    cur = await conn.execute(
        """
        SELECT id, payload
        FROM outbox
        WHERE topic = %s AND sent_at IS NULL
        ORDER BY id
        LIMIT %s
        FOR UPDATE SKIP LOCKED
        """,
        (topic, limit),
    )
    return await cur.fetchall()


async def mark_outbox_sent(conn: AsyncConnection, outbox_id: int) -> None:
    await conn.execute("UPDATE outbox SET sent_at = now() WHERE id = %s", (outbox_id,))


# --- policy search ---


def to_vector_literal(vector: list[float]) -> str:
    """pgvector's text form, e.g. '[0.1,0.2]'. Send it with a `%s::vector` cast so Postgres converts it."""
    return "[" + ",".join(str(x) for x in vector) + "]"


async def search_policy_by_vector(
    pool: AsyncConnectionPool, merchant_id: str, query_vector: list[float], limit: int = 3
) -> list[dict]:
    """The merchant's policy chunks nearest in meaning to `query_vector`, closest first.

    `distance` is cosine distance: 0 means the same direction, larger means less related (it can reach 2).
    It always returns the nearest `limit` chunks, however unrelated: judging relevance is the caller's job.
    """
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT title, body, embedding <=> %s::vector AS distance
            FROM policy_chunks
            WHERE merchant_id = %s AND embedding IS NOT NULL
            ORDER BY distance
            LIMIT %s
            """,
            (to_vector_literal(query_vector), merchant_id, limit),
        )
        return await cur.fetchall()


# --- linked-account notes ---


async def get_order_customer_id(pool: AsyncConnectionPool, merchant_id: str, order_id: str) -> str | None:
    """Who placed this order (None if the order has no customer on record, or does not exist for this merchant)."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT customer_id FROM orders WHERE merchant_id = %s AND id = %s", (merchant_id, order_id))
        row = await cur.fetchone()
        return row["customer_id"] if row else None


async def set_risk_flags(pool: AsyncConnectionPool, merchant_id: str, refund_id: int, flags: list[dict]) -> bool:
    """Store the advisory notes on a refund request. False if no such request exists for this merchant."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            UPDATE refund_requests SET risk_flags = %s
            WHERE id = %s AND merchant_id = %s
            RETURNING id
            """,
            (Jsonb(flags), refund_id, merchant_id),
        )
        return await cur.fetchone() is not None


# --- what the operator screens read ---

# Each refund request with its order and customer, and when anything last happened to it. Callers add conditions.
_REFUND_ROWS = """
    SELECT r.id AS refund_id, r.order_id, r.amount_cents, r.status, r.created_at, r.risk_flags, r.provider_refund_id,
           o.item, o.total_cents AS order_total_cents, o.customer_id, c.name AS customer_name,
           (SELECT max(e.created_at) FROM refund_events e WHERE e.refund_id = r.id) AS updated_at
    FROM refund_requests r
    JOIN orders o ON o.merchant_id = r.merchant_id AND o.id = r.order_id
    LEFT JOIN customers c ON c.merchant_id = o.merchant_id AND c.id = o.customer_id
    WHERE r.merchant_id = %(merchant)s
"""


async def list_refunds(
    pool: AsyncConnectionPool, merchant_id: str, status: str | None = None, limit: int | None = 200
) -> list[dict]:
    """This merchant's refund requests, newest first, optionally only those in one status. `limit=None`: all of them."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            _REFUND_ROWS
            + """
              AND (%(status)s::text IS NULL OR r.status = %(status)s)
            ORDER BY r.id DESC
            LIMIT %(limit)s  -- LIMIT NULL means no limit
            """,
            {"merchant": merchant_id, "status": status, "limit": limit},
        )
        return await cur.fetchall()


async def get_refund_detail(pool: AsyncConnectionPool, merchant_id: str, refund_id: int) -> dict | None:
    """One refund request with its audit trail (oldest event first). None if it is not this merchant's."""
    async with pool.connection() as conn:
        cur = await conn.execute(_REFUND_ROWS + " AND r.id = %(refund)s", {"merchant": merchant_id, "refund": refund_id})
        row = await cur.fetchone()
        if row is None:
            return None
        cur = await conn.execute(
            "SELECT event, actor, note, created_at FROM refund_events WHERE refund_id = %s ORDER BY id", (refund_id,)
        )
        return {**row, "events": await cur.fetchall()}


async def list_activity(pool: AsyncConnectionPool, merchant_id: str, limit: int = 20) -> list[dict]:
    """What happened to this merchant's refunds, newest first: each request being filed by the agent, then each event in
    its audit log (approved, rejected, issued)."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            -- 1. This merchant's refund requests, with what the feed shows about each.
            WITH requests AS (
                SELECT r.id, r.created_at, r.amount_cents, r.provider_refund_id, o.item, c.name AS customer_name,
                       EXISTS (SELECT 1 FROM jsonb_array_elements(r.risk_flags) AS note
                               WHERE note->>'code' IN ('linked_customers', 'linked_refunds')) AS has_linked_accounts
                FROM refund_requests r
                JOIN orders o ON o.merchant_id = r.merchant_id AND o.id = r.order_id
                LEFT JOIN customers c ON c.merchant_id = o.merchant_id AND c.id = o.customer_id
                WHERE r.merchant_id = %(merchant)s
            ),
            -- 2. Everything that happened to them: the filing, then the audit log.
            timeline AS (
                SELECT id AS refund_id, created_at AS at, 'filed' AS event, 'agent' AS actor, NULL AS note, 0 AS seq
                FROM requests
                UNION ALL
                SELECT refund_id, created_at, event, actor, note, id
                FROM refund_events
                WHERE refund_id IN (SELECT id FROM requests)
            )
            -- 3. Each happening with its request's details, newest first.
            SELECT t.at, t.event, t.actor, t.note, t.refund_id, r.amount_cents, r.item, r.customer_name,
                   t.event = 'filed' AND r.has_linked_accounts AS flagged,
                   CASE WHEN t.event = 'issued' THEN r.provider_refund_id END AS provider_refund_id
            FROM timeline t
            JOIN requests r ON r.id = t.refund_id
            ORDER BY t.at DESC, t.seq DESC  -- seq: at the same instant, a filing comes before the events that follow it
            LIMIT %(limit)s
            """,
            {"merchant": merchant_id, "limit": limit},
        )
        return await cur.fetchall()


async def get_customer(pool: AsyncConnectionPool, merchant_id: str, customer_id: str) -> dict | None:
    """One customer with their orders (card, address, latest refund request on each), newest first. None if not this merchant's."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id AS customer_id, name FROM customers WHERE merchant_id = %s AND id = %s", (merchant_id, customer_id)
        )
        customer = await cur.fetchone()
        if customer is None:
            return None
        cur = await conn.execute(
            """
            SELECT o.id AS order_id, o.item, o.total_cents, o.status, o.delivered_at,
                   pm.last4 AS card_last4, a.display AS address,
                   latest.id AS refund_id, latest.amount_cents AS refund_cents, latest.status AS refund_status
            FROM orders o
            LEFT JOIN payment_methods pm ON pm.merchant_id = o.merchant_id AND pm.id = o.payment_method_id
            LEFT JOIN addresses a ON a.merchant_id = o.merchant_id AND a.id = o.shipping_address_id
            LEFT JOIN LATERAL (  -- the order's most recent refund request, if any
                SELECT id, amount_cents, status FROM refund_requests
                WHERE merchant_id = o.merchant_id AND order_id = o.id
                ORDER BY id DESC
                LIMIT 1
            ) latest ON true
            WHERE o.merchant_id = %s AND o.customer_id = %s
            ORDER BY o.delivered_at DESC NULLS FIRST, o.id
            """,
            (merchant_id, customer_id),
        )
        return {**customer, "orders": await cur.fetchall()}


async def refund_status_counts(pool: AsyncConnectionPool, merchant_id: str) -> dict:
    """How many requests, and how much money, in each status; plus the payout jobs not yet handed to the queue."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT status, count(*) AS n, coalesce(sum(amount_cents), 0) AS cents
            FROM refund_requests
            WHERE merchant_id = %s
            GROUP BY status
            """,
            (merchant_id,),
        )
        by_status = {row["status"]: {"count": row["n"], "amount_cents": row["cents"]} for row in await cur.fetchall()}
        cur = await conn.execute(
            """
            SELECT count(*) AS n, min(created_at) AS oldest
            FROM outbox
            WHERE sent_at IS NULL AND topic = %s AND payload->>'merchant_id' = %s
            """,
            (PAYOUT_TOPIC, merchant_id),
        )
        outbox = await cur.fetchone()
        return {"by_status": by_status, "outbox_unsent": outbox["n"], "outbox_oldest": outbox["oldest"]}
