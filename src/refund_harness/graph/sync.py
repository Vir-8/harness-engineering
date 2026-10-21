"""Copies one merchant's data from Postgres into the Neo4j graph, safely to repeat.

The graph is a derived copy ("read model"): Postgres is the truth, and this can be re-run at any time. It only ADDS and
UPDATES (MERGE); a row that has disappeared from Postgres stays in the graph until a rebuild removes it.

The model:
    (:Customer)-[:USES]->(:Card)          this customer paid with this card (seen on at least one order)
    (:Customer)-[:SHIPS_TO]->(:Address)   something was shipped there for this customer
    (:Customer)-[:PLACED]->(:Order)
    (:Order)-[:HAS_REFUND]->(:Refund)
Every node carries merchant_id, and EVERY lookup below includes it: a card called 'pm-1111' exists once per merchant.
"""

from neo4j import AsyncDriver, AsyncManagedTransaction
from psycopg_pool import AsyncConnectionPool

from refund_harness.graph.source import GraphData, read_graph_data

LABELS = ("Customer", "Card", "Address", "Order", "Refund")

# One uniqueness rule per label. It does two jobs: no two nodes can share an identity, and it makes MERGE's lookup fast.
CONSTRAINTS = [
    f"CREATE CONSTRAINT {label.lower()}_identity IF NOT EXISTS FOR (n:{label}) REQUIRE (n.merchant_id, n.id) IS UNIQUE"
    for label in LABELS
]

# Cypher cannot take a label as a parameter, so there is one query per kind. Values always travel as parameters.
# MERGE on the IDENTITY only (merchant_id + id) and SET the rest: if the facts were part of the MERGE pattern, a changed
# name would no longer match and a second node would be created.
NODE_QUERIES = {
    "customers": """
        UNWIND $rows AS row
        MERGE (n:Customer {merchant_id: $merchant_id, id: row.id})
        SET n.name = row.name""",
    "cards": """
        UNWIND $rows AS row
        MERGE (n:Card {merchant_id: $merchant_id, id: row.id})
        SET n.last4 = row.last4""",
    "addresses": """
        UNWIND $rows AS row
        MERGE (n:Address {merchant_id: $merchant_id, id: row.id})
        SET n.normalized = row.normalized""",
    "orders": """
        UNWIND $rows AS row
        MERGE (n:Order {merchant_id: $merchant_id, id: row.id})
        SET n.total_cents = row.total_cents, n.status = row.status""",
    "refunds": """
        UNWIND $rows AS row
        MERGE (n:Refund {merchant_id: $merchant_id, id: row.id})
        SET n.amount_cents = row.amount_cents, n.status = row.status""",
}

# A relationship's two ends are found by (merchant_id, id) too. Leave merchant_id out of one of these MATCHes and a card
# 'pm-1111' of ANOTHER merchant would be linked in as well.
RELATIONSHIP_QUERIES = {
    "customer_cards": """
        UNWIND $rows AS row
        MATCH (a:Customer {merchant_id: $merchant_id, id: row.customer_id})
        MATCH (b:Card {merchant_id: $merchant_id, id: row.card_id})
        MERGE (a)-[:USES]->(b)""",
    "customer_addresses": """
        UNWIND $rows AS row
        MATCH (a:Customer {merchant_id: $merchant_id, id: row.customer_id})
        MATCH (b:Address {merchant_id: $merchant_id, id: row.address_id})
        MERGE (a)-[:SHIPS_TO]->(b)""",
    "placed": """
        UNWIND $rows AS row
        MATCH (a:Customer {merchant_id: $merchant_id, id: row.customer_id})
        MATCH (b:Order {merchant_id: $merchant_id, id: row.id})
        MERGE (a)-[:PLACED]->(b)""",
    "has_refund": """
        UNWIND $rows AS row
        MATCH (a:Order {merchant_id: $merchant_id, id: row.order_id})
        MATCH (b:Refund {merchant_id: $merchant_id, id: row.id})
        MERGE (a)-[:HAS_REFUND]->(b)""",
}


def plan(data: GraphData) -> list[tuple[str, str, list[dict]]]:
    """(name, query, rows) in the order they must run: ALL nodes first, because a relationship needs both ends to exist."""
    orders_with_a_customer = [row for row in data.orders if row["customer_id"] is not None]
    nodes = [(name, query, getattr(data, name)) for name, query in NODE_QUERIES.items()]
    relationships = [
        ("customer_cards", RELATIONSHIP_QUERIES["customer_cards"], data.customer_cards),
        ("customer_addresses", RELATIONSHIP_QUERIES["customer_addresses"], data.customer_addresses),
        ("placed", RELATIONSHIP_QUERIES["placed"], orders_with_a_customer),
        ("has_refund", RELATIONSHIP_QUERIES["has_refund"], data.refunds),
    ]
    return nodes + relationships


async def ensure_constraints(driver: AsyncDriver) -> None:
    """Separate from the data load: Neo4j will not change the schema in a transaction that has already written data."""
    for statement in CONSTRAINTS:
        await driver.execute_query(statement)


async def _load(tx: AsyncManagedTransaction, merchant_id: str, steps: list[tuple[str, str, list[dict]]]) -> dict[str, int]:
    loaded = {}
    for name, query, rows in steps:
        if rows:
            await tx.run(query, merchant_id=merchant_id, rows=rows)
        loaded[name] = len(rows)
    return loaded


async def sync_merchant(pool: AsyncConnectionPool, driver: AsyncDriver, merchant_id: str) -> dict[str, int]:
    """Bring one merchant's slice of the graph up to date with Postgres. Returns how many rows of each kind were loaded.

    All the writes are one transaction: the graph never shows half a sync.
    """
    data = await read_graph_data(pool, merchant_id)
    await ensure_constraints(driver)
    async with driver.session() as session:
        return await session.execute_write(_load, merchant_id, plan(data))
