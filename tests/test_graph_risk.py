"""Advisory linked-account notes from the graph: how they are worked out (assess_customer / flags_from), and what happens
to them on a real refund request: stored for the approver, never shown to the model, never able to block a refund.

The ring (graph-a): ana, ben and cy share a card; ben and eve share an address; eve and fay share another card; dee is alone.
Within 2 customer-steps: ana reaches ben, cy, eve (3); fay reaches eve and ben (2); dee reaches nobody.
"""

import asyncio
import time
from datetime import UTC, datetime, timedelta

import pytest
from graph_helpers import A, B
from helpers import ACME, GLOBEX, REVIEWER, approve, request
from neo4j import AsyncGraphDatabase

from refund_harness.agent.tools import ToolRegistry
from refund_harness.data.repository import set_risk_flags
from refund_harness.graph import risk
from refund_harness.graph.risk import (
    GRAPH_UNAVAILABLE,
    NO_CUSTOMER,
    assess_customer,
    flags_from,
)
from refund_harness.refund_tools import build_registry
from refund_harness.retrieval.embedder import FakeEmbedder

DELIVERED = datetime(2026, 10, 15, tzinfo=UTC)
NOW = DELIVERED + timedelta(days=3)
BEN_ORDER = {"order_id": "O2", "amount_cents": 1000}


@pytest.fixture
async def synced(world):
    await world.sync(A)
    return world


@pytest.fixture
async def ring(world):
    """graph-a delivered 3 days before NOW (so ben's O2 is eligible) and synced; builds a registry with or without the graph."""
    async with world.pool.connection() as conn:
        await conn.execute("UPDATE orders SET delivered_at = %s WHERE merchant_id = %s", (DELIVERED, A))
    await world.sync(A)

    def registry(graph_driver) -> ToolRegistry:
        return build_registry(world.pool, A, FakeEmbedder(), now=lambda: NOW, graph_driver=graph_driver)

    world.registry = registry
    return world


def codes(flags: list[dict]) -> list[str]:
    return [flag["code"] for flag in flags]


async def stored_flags(pool, merchant: str, order: str):
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT risk_flags FROM refund_requests WHERE merchant_id = %s AND order_id = %s", (merchant, order))
        return (await cur.fetchone())["risk_flags"]


# --- the thresholds (pure) ---

@pytest.mark.parametrize(
    "customers, refunds, expected",
    [
        (0, 0, []),
        (1, 1, []),  # one linked customer, one refund: not worth a note
        (2, 0, ["linked_customers"]),
        (0, 2, ["linked_refunds"]),
        (5, 4, ["linked_customers", "linked_refunds"]),
    ],
)
def test_notes_appear_only_at_the_thresholds(customers, refunds, expected):
    assert codes(flags_from(customers, refunds)) == expected


# --- against the real graph ---

async def test_customer_in_a_ring_is_flagged(synced):
    flags = await assess_customer(synced.driver, A, "ana")

    assert codes(flags) == ["linked_customers"]
    assert "3 other customers" in flags[0]["message"]  # ben and cy by card, eve through ben's address


async def test_linked_customers_refunds_are_noted(synced):
    async with synced.pool.connection() as conn:
        await conn.execute("INSERT INTO refund_requests (merchant_id, order_id, amount_cents) VALUES (%s, 'O2', 1000), (%s, 'O3', 1000)", (A, A))
    await synced.sync(A)

    flags = await assess_customer(synced.driver, A, "ana")

    assert codes(flags) == ["linked_customers", "linked_refunds"]
    assert "2 refund requests" in flags[1]["message"]  # ben's and cy's: ana's own request (the seed's, on O1) does not count


async def test_other_merchants_ring_does_not_count(synced):
    await synced.sync(B)

    flags = await assess_customer(synced.driver, A, "ana")

    assert "3 other customers" in flags[0]["message"]  # not 6: graph-b has the same ids and the same shape


async def test_customer_is_never_linked_to_themselves(synced):
    """dee now shares ana's card AND ana's address, so there is a loop: ana, card, dee, address, back to ana."""
    async with synced.pool.connection() as conn:
        cur = await conn.execute("SELECT id FROM addresses WHERE merchant_id = %s AND normalized = '9 pine ave'", (A,))
        pine = (await cur.fetchone())["id"]
        await conn.execute(
            "INSERT INTO orders (merchant_id, id, item, total_cents, status, customer_id, payment_method_id, shipping_address_id) "
            "VALUES (%s, 'O7', 'thing', 1000, 'delivered', 'dee', 'pm-1111', %s)", (A, pine),
        )
    await synced.sync(A)

    flags = await assess_customer(synced.driver, A, "ana")

    assert "4 other customers" in flags[0]["message"]  # ben, cy, eve and dee: not ana herself as a fifth


async def test_note_shows_how_customers_are_connected(synced):
    [note] = await assess_customer(synced.driver, A, "fay")

    # Also pins how far the check looks, 2 customer-steps: eve (1 step) and ben (2) are here; ana and cy (3) are not.
    assert note["links"] == [
        {"customer_id": "eve", "name": "Eve", "refunds": 0, "via": [{"kind": "card", "label": "3333"}]},
        {
            "customer_id": "ben",
            "name": "Ben",
            "refunds": 0,
            "via": [{"kind": "card", "label": "3333"}, {"kind": "customer", "label": "Eve"}, {"kind": "address", "label": "12 elm st"}],
        },
    ]


async def test_note_shows_each_linked_customers_refunds(synced):
    [note] = await assess_customer(synced.driver, A, "ben")

    assert {link["customer_id"]: link["refunds"] for link in note["links"]}["ana"] == 1  # the seed's request on ana's O1


# --- the check must never break a refund ---

class SlowDriver:
    async def execute_query(self, *args, **kwargs):
        await asyncio.sleep(5)


class BrokenDriver:
    async def execute_query(self, *args, **kwargs):
        raise RuntimeError("the graph exploded")


@pytest.mark.parametrize(
    ("graph", "customer", "expected"),
    [
        (None, None, NO_CUSTOMER),  # no graph is even asked
        (SlowDriver(), "ana", GRAPH_UNAVAILABLE),
        (BrokenDriver(), "ana", GRAPH_UNAVAILABLE),
    ],
    ids=["order_has_no_customer", "graph_answers_too_slowly", "graph_raises"],
)
async def test_note_says_when_the_check_cannot_run(monkeypatch, graph, customer, expected):
    monkeypatch.setattr(risk, "GRAPH_TIMEOUT_S", 0.05)
    started = time.perf_counter()

    assert await assess_customer(graph, A, customer) == [expected]
    assert time.perf_counter() - started < 1  # a slow graph is given up on after 50 ms, not after its own 5 seconds


async def test_neo4j_down_still_creates_the_refund(ring, monkeypatch):
    monkeypatch.setattr(risk, "GRAPH_TIMEOUT_S", 0.5)
    dead = AsyncGraphDatabase.driver("bolt://localhost:1", auth=("neo4j", "nothing"))
    started = time.perf_counter()
    try:
        text = await ring.registry(dead).get("submit_order_refund").run(BEN_ORDER)
    finally:
        await dead.close()

    # The driver itself would keep retrying a dead server for about 30 seconds: it is OUR time limit that ends the wait.
    assert time.perf_counter() - started < 5
    assert "pending human approval" in text
    assert await stored_flags(ring.pool, A, "O2") == [GRAPH_UNAVAILABLE]


# --- on a real refund request ---

async def test_notes_go_to_the_approver_never_the_model(ring):
    text = await ring.registry(ring.driver).get("submit_order_refund").run(BEN_ORDER)

    flags = await stored_flags(ring.pool, A, "O2")
    assert codes(flags) == ["linked_customers"]  # ben is linked to ana, cy, eve and fay within 2 steps
    assert "4 other customers" in flags[0]["message"]
    assert flags[0]["message"] not in text
    assert "linked" not in text.lower() and "card" not in text.lower() and "address" not in text.lower()
    assert "pending human approval" in text  # the reply is exactly what it was before the check existed


@pytest.mark.parametrize(
    ("with_graph", "stored"),
    [(False, None), (True, [])],
    ids=["no_graph_is_NULL_never_checked", "checked_and_clean_is_an_empty_list"],
)
async def test_unchecked_differs_from_checked_clean(ring, with_graph, stored):
    registry = ring.registry(ring.driver if with_graph else None)

    await registry.get("submit_order_refund").run({"order_id": "O4", "amount_cents": 1000})  # dee, who is linked to nobody

    assert await stored_flags(ring.pool, A, "O4") == stored


async def test_repeat_request_keeps_the_notes(ring):
    tool = ring.registry(ring.driver).get("submit_order_refund")
    await tool.run(BEN_ORDER)
    first = await stored_flags(ring.pool, A, "O2")

    text = await tool.run(BEN_ORDER)

    assert "already exists" in text
    assert await stored_flags(ring.pool, A, "O2") == first


async def test_notes_stay_with_the_merchants_request(refunds, pool):
    refund_id = await request(pool, ACME, "A100", 5900)

    assert await set_risk_flags(pool, GLOBEX, refund_id, [{"code": "x", "message": "y"}]) is False
    assert await stored_flags(pool, ACME, "A100") is None
    assert await set_risk_flags(pool, ACME, refund_id, [{"code": "x", "message": "y"}]) is True


# --- the approver's screens ---

async def test_approver_sees_each_request_with_notes(client, refunds, pool):
    linked = await request(pool, ACME, "A100", 5900)
    await set_risk_flags(pool, ACME, linked, [{"code": "linked_customers", "message": "Linked to 3 other customers."}])
    not_checkable = await request(pool, ACME, "A200", 3500)
    await set_risk_flags(pool, ACME, not_checkable, [GRAPH_UNAVAILABLE])
    unchecked = await request(pool, ACME, "A300", 8900)
    decided = await request(pool, ACME, "B100", 4500)
    await approve(pool, decided)

    pending = (await client.get("/refunds?status=pending", headers=REVIEWER)).json()
    activity = (await client.get("/activity", headers=REVIEWER)).json()

    assert {row["refund_id"]: row["risk_flags"] for row in pending} == {  # the approved one is no longer waiting
        linked: [{"code": "linked_customers", "message": "Linked to 3 other customers.", "links": None}],
        not_checkable: [{**GRAPH_UNAVAILABLE, "links": None}],
        unchecked: None,  # never checked, and the screen says so
    }
    # the activity feed flags only real findings: "the check could not run" is not one
    filed = {item["refund_id"]: item["flagged"] for item in activity if item["event"] == "filed"}
    assert filed == {linked: True, not_checkable: False, unchecked: False, decided: False}
