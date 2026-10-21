"""sync_merchant: Postgres to Neo4j. Safe to repeat, never mixes merchants, all-or-nothing.

Two throwaway merchants (graph-a, graph-b) get the same ring and the same ids; see graph_helpers.py.
"""

import pytest
from graph_helpers import A, B, World, seed_ring, wipe
from neo4j.exceptions import ConstraintError

from refund_harness.graph import sync as sync_module
from refund_harness.graph.driver import create_graph_driver

EXPECTED_SHAPE = {
    "Customer": 6, "Card": 3, "Address": 5, "Order": 6, "Refund": 1,
    "USES": 6, "SHIPS_TO": 6, "PLACED": 6, "HAS_REFUND": 1,
}


@pytest.fixture
async def world(pool):
    driver = create_graph_driver()
    await wipe(pool, driver)  # in case an earlier run crashed halfway
    await seed_ring(pool, A)
    await seed_ring(pool, B)
    yield World(pool, driver)
    await wipe(pool, driver)
    await driver.close()


# --- what lands in the graph ---

async def test_the_graph_has_a_node_for_every_row_and_a_link_for_every_connection(world):
    await world.sync(A)

    assert await world.shape(A) == EXPECTED_SHAPE


async def test_the_sync_reports_how_many_rows_it_loaded(world):
    loaded = await world.sync(A)

    assert loaded == {
        "customers": 6, "cards": 3, "addresses": 5, "orders": 6, "refunds": 1,
        "customer_cards": 6, "customer_addresses": 6, "placed": 6, "has_refund": 1,
    }


async def test_a_customer_with_several_orders_on_one_card_gets_one_link_not_several(world):
    async with world.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO orders (merchant_id, id, item, total_cents, status, customer_id, payment_method_id) "
            "VALUES (%s, 'O7', 'thing', 1000, 'delivered', 'ana', 'pm-1111')", (A,)
        )

    loaded = await world.sync(A)

    assert loaded["customer_cards"] == 6  # six distinct (customer, card) pairs, though there are now seven orders
    assert await world.count("MATCH (:Customer {merchant_id: $m, id: 'ana'})-[r:USES]->(:Card) RETURN count(r) AS n", m=A) == 1
    assert await world.count("MATCH (:Customer {merchant_id: $m, id: 'ana'})-[r:PLACED]->() RETURN count(r) AS n", m=A) == 2


async def test_an_order_with_no_customer_recorded_is_still_a_node_but_has_no_placed_link(world):
    async with world.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO orders (merchant_id, id, item, total_cents, status) VALUES (%s, 'O7', 'thing', 1000, 'placed')", (A,)
        )

    await world.sync(A)

    assert await world.count("MATCH (n:Order {merchant_id: $m}) RETURN count(n) AS n", m=A) == 7
    assert await world.count("MATCH (:Customer {merchant_id: $m})-[r:PLACED]->() RETURN count(r) AS n", m=A) == 6


# --- the questions the graph exists to answer ---

async def test_the_ring_is_there_ana_ben_and_cy_share_a_card(world):
    await world.sync(A)

    sharers = await world.values(
        "MATCH (:Customer {merchant_id: $m, id: 'ana'})-[:USES]->(:Card)<-[:USES]-(o:Customer) RETURN o.id", m=A
    )

    assert sharers == ["ben", "cy"]


async def test_ben_and_eve_share_an_address(world):
    await world.sync(A)

    sharers = await world.values(
        "MATCH (:Customer {merchant_id: $m, id: 'ben'})-[:SHIPS_TO]->(:Address)<-[:SHIPS_TO]-(o:Customer) RETURN o.id", m=A
    )

    assert sharers == ["eve"]


async def test_dee_is_connected_to_nobody(world):
    await world.sync(A)

    through_card = await world.values("MATCH (:Customer {merchant_id: $m, id: 'dee'})-[:USES]->()<-[:USES]-(o) RETURN o.id", m=A)
    through_address = await world.values("MATCH (:Customer {merchant_id: $m, id: 'dee'})-[:SHIPS_TO]->()<-[:SHIPS_TO]-(o) RETURN o.id", m=A)

    assert through_card == [] and through_address == []


async def test_the_refund_hangs_off_its_order(world):
    await world.sync(A)

    refunds = await world.rows(
        "MATCH (:Order {merchant_id: $m, id: 'O1'})-[:HAS_REFUND]->(r:Refund) RETURN r.amount_cents AS cents, r.status AS status", m=A
    )

    assert refunds == [{"cents": 1000, "status": "pending"}]


# --- repeating it ---

async def test_running_the_sync_twice_changes_nothing(world):
    await world.sync(A)
    first = await world.shape(A)

    await world.sync(A)

    assert await world.shape(A) == first == EXPECTED_SHAPE


async def test_a_changed_fact_updates_the_node_instead_of_creating_a_second_one(world):
    await world.sync(A)
    async with world.pool.connection() as conn:
        await conn.execute("UPDATE customers SET name = 'Ana Renamed' WHERE merchant_id = %s AND id = 'ana'", (A,))

    await world.sync(A)

    assert await world.count("MATCH (n:Customer {merchant_id: $m}) RETURN count(n) AS n", m=A) == 6
    assert await world.values("MATCH (n:Customer {merchant_id: $m, id: 'ana'}) RETURN n.name", m=A) == ["Ana Renamed"]


async def test_new_rows_arrive_on_the_next_sync(world):
    await world.sync(A)
    async with world.pool.connection() as conn:
        await conn.execute("INSERT INTO customers (merchant_id, id, name) VALUES (%s, 'gil', 'Gil')", (A,))

    await world.sync(A)

    assert await world.count("MATCH (n:Customer {merchant_id: $m}) RETURN count(n) AS n", m=A) == 7


# --- tenants ---

async def test_two_merchants_with_identical_ids_get_two_separate_graphs(world):
    await world.sync(A)
    await world.sync(B)

    assert await world.shape(A) == EXPECTED_SHAPE and await world.shape(B) == EXPECTED_SHAPE
    assert await world.count("MATCH (n:Card {id: 'pm-1111'}) RETURN count(n) AS n") >= 2  # one per merchant, not one shared


async def test_no_link_ever_crosses_from_one_merchant_to_another(world):
    await world.sync(A)
    await world.sync(B)

    crossing = await world.count("MATCH (a)-[r]->(b) WHERE a.merchant_id <> b.merchant_id RETURN count(r) AS n")

    assert crossing == 0


async def test_each_merchants_ring_stays_its_own_size(world):
    await world.sync(A)
    await world.sync(B)

    sharers = await world.values(
        "MATCH (:Customer {merchant_id: $m, id: 'ana'})-[:USES]->(:Card)<-[:USES]-(o:Customer) RETURN o.id", m=B
    )

    assert sharers == ["ben", "cy"]  # not ['ben', 'ben', 'cy', 'cy']: the other merchant's card did not join in


async def test_syncing_one_merchant_does_not_touch_another(world):
    await world.sync(A)

    assert await world.shape(B) == {}


async def test_a_merchant_with_no_data_loads_nothing_and_does_not_fail(world):
    loaded = await world.sync("graph-nobody")

    assert set(loaded.values()) == {0}
    assert await world.count("MATCH (n {merchant_id: 'graph-nobody'}) RETURN count(n) AS n") == 0


# --- the schema and all-or-nothing ---

async def test_each_kind_of_node_has_a_uniqueness_rule_and_it_is_enforced(world):
    await world.sync(A)

    rules = await world.rows("SHOW CONSTRAINTS YIELD name, labelsOrTypes WHERE name ENDS WITH '_identity' RETURN labelsOrTypes[0] AS label")
    assert sorted(r["label"] for r in rules) == ["Address", "Card", "Customer", "Order", "Refund"]

    with pytest.raises(ConstraintError):
        await world.driver.execute_query("CREATE (:Customer {merchant_id: $m, id: 'ana'})", m=A)


async def test_if_a_step_fails_nothing_at_all_is_written(world, monkeypatch):
    monkeypatch.setitem(sync_module.RELATIONSHIP_QUERIES, "has_refund", "THIS IS NOT CYPHER")

    with pytest.raises(Exception):
        await world.sync(A)

    assert await world.count("MATCH (n {merchant_id: $m}) RETURN count(n) AS n", m=A) == 0  # the nodes loaded before it were undone
