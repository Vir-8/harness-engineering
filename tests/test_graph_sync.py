"""sync_merchant and rebuild_merchant: Postgres to Neo4j. Safe to repeat, never mixes merchants, all-or-nothing.

Two throwaway merchants (graph-a, graph-b) get the same ring and the same ids; see graph_helpers.py.
"""

import pytest
from graph_helpers import A, B
from neo4j.exceptions import ConstraintError, CypherSyntaxError

from refund_harness.graph import sync as sync_module
from refund_harness.graph.source import read_graph_data
from refund_harness.graph.sync import rebuild_merchant

EXPECTED_SHAPE = {
    "Customer": 6, "Card": 3, "Address": 5, "Order": 6, "Refund": 1,
    "USES": 6, "SHIPS_TO": 6, "PLACED": 6, "HAS_REFUND": 1,
}


# --- what lands in the graph ---

async def test_every_row_and_connection_is_in_the_graph(world):
    await world.sync(A)

    assert await world.shape(A) == EXPECTED_SHAPE


async def test_order_without_a_customer_has_no_link(world):
    async with world.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO orders (merchant_id, id, item, total_cents, status) VALUES (%s, 'O7', 'thing', 1000, 'placed')", (A,)
        )

    await world.sync(A)

    assert await world.count("MATCH (n:Order {merchant_id: $m}) RETURN count(n) AS n", m=A) == 7
    assert await world.count("MATCH (:Customer {merchant_id: $m})-[r:PLACED]->() RETURN count(r) AS n", m=A) == 6


async def test_ids_are_always_strings(world):
    # A graph key of 5 is not equal to '5': mixed types would make MERGE create duplicates.
    data = await read_graph_data(world.pool, A)

    assert all(isinstance(a["id"], str) for a in data.addresses)
    assert all(isinstance(pair["address_id"], str) for pair in data.customer_addresses)
    assert all(isinstance(r["id"], str) for r in data.refunds)


# --- repeating it ---

async def test_syncing_twice_changes_nothing(world):
    await world.sync(A)
    first = await world.shape(A)

    await world.sync(A)

    assert await world.shape(A) == first == EXPECTED_SHAPE


async def test_changed_fact_updates_the_node(world):
    await world.sync(A)
    async with world.pool.connection() as conn:
        await conn.execute("UPDATE customers SET name = 'Ana Renamed' WHERE merchant_id = %s AND id = 'ana'", (A,))

    await world.sync(A)

    assert await world.count("MATCH (n:Customer {merchant_id: $m}) RETURN count(n) AS n", m=A) == 6
    assert await world.values("MATCH (n:Customer {merchant_id: $m, id: 'ana'}) RETURN n.name", m=A) == ["Ana Renamed"]


# --- tenants ---

async def test_merchants_with_identical_ids_stay_apart(world):
    await world.sync(A)
    await world.sync(B)

    assert await world.shape(A) == EXPECTED_SHAPE and await world.shape(B) == EXPECTED_SHAPE
    assert await world.count("MATCH (n:Card {id: 'pm-1111'}) RETURN count(n) AS n") >= 2  # one per merchant, not one shared
    assert await world.count("MATCH (a)-[r]->(b) WHERE a.merchant_id <> b.merchant_id RETURN count(r) AS n") == 0


# --- the schema and all-or-nothing ---

async def test_every_node_kind_is_unique_per_merchant(world):
    await world.sync(A)

    rules = await world.rows("SHOW CONSTRAINTS YIELD name, labelsOrTypes WHERE name ENDS WITH '_identity' RETURN labelsOrTypes[0] AS label")
    assert sorted(r["label"] for r in rules) == ["Address", "Card", "Customer", "Order", "Refund"]

    with pytest.raises(ConstraintError):
        await world.driver.execute_query("CREATE (:Customer {merchant_id: $m, id: 'ana'})", m=A)


async def sync(world) -> None:
    await world.sync(A)


async def rebuild(world) -> None:
    await rebuild_merchant(world.pool, world.driver, A)


@pytest.mark.parametrize(
    ("graph_before", "write"),
    [({}, sync), (EXPECTED_SHAPE, rebuild)],
    ids=["a_first_sync_leaves_no_nodes", "a_rebuild_leaves_the_old_graph_not_a_blank_map"],
)
async def test_failed_sync_leaves_the_graph_unchanged(world, monkeypatch, graph_before, write):
    if graph_before:
        await world.sync(A)
    assert await world.shape(A) == graph_before
    monkeypatch.setitem(sync_module.RELATIONSHIP_QUERIES, "has_refund", "THIS IS NOT CYPHER")  # the LAST step fails

    with pytest.raises(CypherSyntaxError):
        await write(world)

    assert await world.shape(A) == graph_before  # everything written before the failing step (a rebuild's delete too) was undone


# --- rebuilding ---

async def test_deleted_rows_go_only_on_rebuild(world):
    await world.sync(A)
    async with world.pool.connection() as conn:
        await conn.execute("DELETE FROM refund_requests WHERE merchant_id = %s", (A,))

    await world.sync(A)
    assert await world.count("MATCH (n:Refund {merchant_id: $m}) RETURN count(n) AS n", m=A) == 1  # MERGE never deletes

    await rebuild_merchant(world.pool, world.driver, A)
    without_refunds = {kind: n for kind, n in EXPECTED_SHAPE.items() if kind not in ("Refund", "HAS_REFUND")}
    assert await world.shape(A) == without_refunds  # kinds with no members are simply absent from the shape


async def test_rebuild_leaves_other_merchants_alone(world):
    await world.sync(A)
    await world.sync(B)
    before = await world.shape(B)

    await rebuild_merchant(world.pool, world.driver, A)

    assert await world.shape(B) == before == EXPECTED_SHAPE
