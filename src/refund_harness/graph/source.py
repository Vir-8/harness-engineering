"""What the graph is built from: one merchant's rows in Postgres, shaped as plain dicts.

Postgres stays the source of truth; the graph is a copy drawn from what this module reads. Every query filters on
merchant_id, so one merchant's data can never end up in another's graph.

Every id comes out as a STRING. Postgres has numeric ids (addresses, refund requests) and text ids (customers, orders),
and a graph key of 5 is not equal to '5': one convention removes a whole class of "why did MERGE create a duplicate".
"""

from dataclasses import dataclass, field

from psycopg_pool import AsyncConnectionPool


@dataclass
class GraphData:
    customers: list[dict] = field(default_factory=list)  # id, name
    cards: list[dict] = field(default_factory=list)  # id, last4
    addresses: list[dict] = field(default_factory=list)  # id, normalized
    orders: list[dict] = field(default_factory=list)  # id, total_cents, status, customer_id (None if not recorded)
    refunds: list[dict] = field(default_factory=list)  # id, order_id, amount_cents, status
    customer_cards: list[dict] = field(default_factory=list)  # customer_id, card_id: distinct pairs seen on orders
    customer_addresses: list[dict] = field(default_factory=list)  # customer_id, address_id: distinct pairs seen on orders


# Each key is a GraphData field name: read_graph_data fills the field of the same name.
QUERIES = {
    "customers": "SELECT id, name FROM customers WHERE merchant_id = %s ORDER BY id",
    "cards": "SELECT id, last4 FROM payment_methods WHERE merchant_id = %s ORDER BY id",
    "addresses": "SELECT id::text AS id, normalized FROM addresses WHERE merchant_id = %s ORDER BY id",
    "orders": "SELECT id, total_cents, status, customer_id FROM orders WHERE merchant_id = %s ORDER BY id",
    "refunds": (
        "SELECT id::text AS id, order_id, amount_cents, status FROM refund_requests WHERE merchant_id = %s ORDER BY id"
    ),
    # DISTINCT: a customer with five orders on one card is ONE link, not five.
    "customer_cards": """
        SELECT DISTINCT customer_id, payment_method_id AS card_id
        FROM orders
        WHERE merchant_id = %s AND customer_id IS NOT NULL AND payment_method_id IS NOT NULL
        ORDER BY 1, 2
    """,
    "customer_addresses": """
        SELECT DISTINCT customer_id, shipping_address_id::text AS address_id
        FROM orders
        WHERE merchant_id = %s AND customer_id IS NOT NULL AND shipping_address_id IS NOT NULL
        ORDER BY 1, 2
    """,
}


async def read_graph_data(pool: AsyncConnectionPool, merchant_id: str) -> GraphData:
    """Everything the graph needs for one merchant, read from ONE consistent snapshot of the database.

    Without the snapshot, an order placed between two of these queries could appear in one list and be missing from
    another, and the graph would then contain a link whose other end is absent.
    """
    data = GraphData()
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")  # must be the first statement
        for name, query in QUERIES.items():
            cur = await conn.execute(query, (merchant_id,))
            setattr(data, name, await cur.fetchall())
    return data
