"""Shared pieces for the graph tests: two throwaway merchants with the SAME ring of customers, and small query helpers.

graph-a and graph-b use identical customer ids, card ids and fingerprints on purpose. If any lookup forgets merchant_id,
the two merchants' graphs get tangled together, and the tenant tests catch it.
"""

from dataclasses import dataclass

from neo4j import AsyncDriver
from psycopg_pool import AsyncConnectionPool

from refund_harness.graph.sync import sync_merchant

A, B = "graph-a", "graph-b"
SANDBOXES = (A, B)

# (order id, customer, card, address text)  ana, ben and cy share card 1111; ben and eve share the elm address;
# eve and fay share card 3333; dee is connected to nobody.
RING = [
    ("O1", "ana", "pm-1111", "9 pine ave"),
    ("O2", "ben", "pm-1111", "12 elm st"),
    ("O3", "cy", "pm-1111", "4 maple rd"),
    ("O4", "dee", "pm-2222", "77 oak ln"),
    ("O5", "eve", "pm-3333", "12 elm st"),
    ("O6", "fay", "pm-3333", "31 birch ct"),
]
CARDS = ["pm-1111", "pm-2222", "pm-3333"]
ADDRESSES = ["9 pine ave", "12 elm st", "4 maple rd", "77 oak ln", "31 birch ct"]


async def seed_ring(pool: AsyncConnectionPool, merchant: str) -> None:
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO merchants (id, name) VALUES (%s, %s)", (merchant, merchant))
        for customer in dict.fromkeys(customer for _, customer, _, _ in RING):
            await conn.execute("INSERT INTO customers (merchant_id, id, name) VALUES (%s, %s, %s)", (merchant, customer, customer.title()))
        for card in CARDS:
            await conn.execute(
                "INSERT INTO payment_methods (merchant_id, id, fingerprint, last4) VALUES (%s, %s, %s, %s)",
                (merchant, card, "fp-" + card[-4:], card[-4:]),
            )
        address_ids = {}
        for text in ADDRESSES:
            cur = await conn.execute(
                "INSERT INTO addresses (merchant_id, normalized, display) VALUES (%s, %s, %s) RETURNING id", (merchant, text, text.title())
            )
            address_ids[text] = (await cur.fetchone())["id"]
        for order, customer, card, address in RING:
            await conn.execute(
                "INSERT INTO orders (merchant_id, id, item, total_cents, status, customer_id, payment_method_id, shipping_address_id) "
                "VALUES (%s, %s, 'thing', 1000, 'delivered', %s, %s, %s)",
                (merchant, order, customer, card, address_ids[address]),
            )
        await conn.execute(
            "INSERT INTO refund_requests (merchant_id, order_id, amount_cents) VALUES (%s, 'O1', 1000)", (merchant,)
        )


async def wipe(pool: AsyncConnectionPool, driver: AsyncDriver) -> None:
    """Remove every trace of the sandbox merchants, from Postgres (children before parents) and from the graph."""
    async with pool.connection() as conn:
        for table in ("refund_requests", "orders", "addresses", "payment_methods", "customers"):
            await conn.execute(f"DELETE FROM {table} WHERE merchant_id = ANY(%s)", (list(SANDBOXES),))  # noqa: S608 (fixed names)
        await conn.execute("DELETE FROM merchants WHERE id = ANY(%s)", (list(SANDBOXES),))
    await driver.execute_query("MATCH (n) WHERE n.merchant_id IN $merchants DETACH DELETE n", merchants=list(SANDBOXES))


@dataclass
class World:
    pool: AsyncConnectionPool
    driver: AsyncDriver

    async def sync(self, merchant: str) -> dict[str, int]:
        return await sync_merchant(self.pool, self.driver, merchant)

    async def rows(self, cypher: str, **params) -> list[dict]:
        records, _, _ = await self.driver.execute_query(cypher, **params)
        return [record.data() for record in records]

    async def values(self, cypher: str, **params) -> list:
        """The first column of every row, sorted: for questions like 'which customers...'."""
        return sorted(next(iter(row.values())) for row in await self.rows(cypher, **params))

    async def count(self, cypher: str, **params) -> int:
        return (await self.rows(cypher, **params))[0]["n"]

    async def shape(self, merchant: str) -> dict[str, int]:
        """How many nodes of each label and relationships of each type this merchant has: a fingerprint of its graph."""
        nodes = await self.rows("MATCH (n {merchant_id: $m}) RETURN labels(n)[0] AS kind, count(*) AS n", m=merchant)
        links = await self.rows("MATCH ({merchant_id: $m})-[r]->() RETURN type(r) AS kind, count(*) AS n", m=merchant)
        return {row["kind"]: row["n"] for row in nodes + links}
