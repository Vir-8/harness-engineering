"""Risk notes on real refund requests: stored for the approver, never shown to the model, never able to block a refund.

Uses the ring merchant (graph-a). Its orders are delivered 3 days before the pinned clock, so ben's order O2 ($10.00) is eligible.
"""

from datetime import datetime, timedelta, timezone

import pytest
from graph_helpers import A
from helpers import ACME, GLOBEX, approve, request
from neo4j import AsyncGraphDatabase

from refund_harness.agent.tools import ToolRegistry
from refund_harness.data.repository import list_pending_refunds, set_risk_flags
from refund_harness.graph import risk
from refund_harness.graph.risk import GRAPH_UNAVAILABLE
from refund_harness.refund_tools import build_registry
from refund_harness.retrieval.embedder import FakeEmbedder

DELIVERED = datetime(2026, 10, 15, tzinfo=timezone.utc)
NOW = DELIVERED + timedelta(days=3)
BEN_ORDER = {"order_id": "O2", "amount_cents": 1000}


@pytest.fixture
async def ring(world):
    """graph-a delivered and synced; returns a builder for a registry with or without the graph check."""
    async with world.pool.connection() as conn:
        await conn.execute("UPDATE orders SET delivered_at = %s WHERE merchant_id = %s", (DELIVERED, A))
    await world.sync(A)

    def registry(graph_driver) -> ToolRegistry:
        return build_registry(world.pool, A, FakeEmbedder(), now=lambda: NOW, graph_driver=graph_driver)

    world.registry = registry
    return world


async def stored_flags(pool, merchant: str, order: str):
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT risk_flags FROM refund_requests WHERE merchant_id = %s AND order_id = %s", (merchant, order))
        return (await cur.fetchone())["risk_flags"]


def codes(flags) -> list[str]:
    return [flag["code"] for flag in flags]


# --- the tool ---

async def test_a_new_refund_is_checked_and_the_notes_are_stored_on_it(ring):
    await ring.registry(ring.driver).get("submit_order_refund").run(BEN_ORDER)

    flags = await stored_flags(ring.pool, A, "O2")

    assert codes(flags) == ["linked_customers"]  # ben is linked to ana, cy, eve and fay within 2 steps
    assert "4 other customers" in flags[0]["message"]


async def test_the_model_is_never_told_what_the_check_found(ring):
    text = await ring.registry(ring.driver).get("submit_order_refund").run(BEN_ORDER)
    [flag] = await stored_flags(ring.pool, A, "O2")

    assert flag["message"] not in text
    assert "linked" not in text.lower() and "card" not in text.lower() and "address" not in text.lower()
    assert "pending human approval" in text  # the reply is exactly what it was before the check existed


async def test_a_refund_is_still_created_when_neo4j_is_down_and_the_approver_is_told_it_was_not_checked(ring, monkeypatch):
    monkeypatch.setattr(risk, "GRAPH_TIMEOUT_S", 0.5)
    dead = AsyncGraphDatabase.driver("bolt://localhost:1", auth=("neo4j", "nothing"))
    try:
        text = await ring.registry(dead).get("submit_order_refund").run(BEN_ORDER)
    finally:
        await dead.close()

    assert "pending human approval" in text
    assert await stored_flags(ring.pool, A, "O2") == [GRAPH_UNAVAILABLE]


async def test_without_a_graph_the_refund_is_created_and_marked_never_checked(ring):
    await ring.registry(None).get("submit_order_refund").run(BEN_ORDER)

    assert await stored_flags(ring.pool, A, "O2") is None  # NULL: never checked, which is not the same as "checked, clean"


async def test_a_customer_with_no_links_is_recorded_as_checked_and_clean(ring):
    await ring.registry(ring.driver).get("submit_order_refund").run({"order_id": "O4", "amount_cents": 1000})  # dee

    assert await stored_flags(ring.pool, A, "O4") == []


async def test_a_repeat_request_does_not_redo_or_wipe_the_notes(ring):
    tool = ring.registry(ring.driver).get("submit_order_refund")
    await tool.run(BEN_ORDER)
    first = await stored_flags(ring.pool, A, "O2")

    text = await tool.run(BEN_ORDER)

    assert "already exists" in text
    assert await stored_flags(ring.pool, A, "O2") == first


async def test_an_ineligible_request_is_not_checked_because_nothing_was_created(ring):
    text = await ring.registry(ring.driver).get("submit_order_refund").run({"order_id": "O2", "amount_cents": 99999})

    assert "not eligible" in text
    async with ring.pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) AS n FROM refund_requests WHERE merchant_id = %s AND order_id = 'O2'", (A,))
        assert (await cur.fetchone())["n"] == 0  # nothing was created, so there was nothing to check


# --- storing and listing ---

async def test_notes_are_only_stored_on_the_merchants_own_request(refunds, pool):
    refund_id = await request(pool, ACME, "A100", 5900)

    assert await set_risk_flags(pool, GLOBEX, refund_id, [{"code": "x", "message": "y"}]) is False
    assert await stored_flags(pool, ACME, "A100") is None
    assert await set_risk_flags(pool, ACME, refund_id, [{"code": "x", "message": "y"}]) is True


async def test_the_approvers_list_has_only_this_merchants_pending_requests(refunds, pool):
    pending = await request(pool, ACME, "A100", 5900)
    approved = await request(pool, ACME, "A200", 3500)
    await approve(pool, approved)
    await request(pool, GLOBEX, "A100", 12000)

    rows = await list_pending_refunds(pool, ACME)

    assert [row["refund_id"] for row in rows] == [pending]


# --- the approver's screen ---

REVIEWER = {"X-Reviewer-Id": "reviewer-1"}


async def test_the_pending_list_shows_each_request_with_its_notes(client, refunds, pool):
    checked = await request(pool, ACME, "A100", 5900)
    await set_risk_flags(pool, ACME, checked, [{"code": "linked_customers", "message": "Linked to 3 other customers."}])
    unchecked = await request(pool, ACME, "A200", 3500)

    response = await client.get("/refunds/pending", headers=REVIEWER)

    by_id = {row["refund_id"]: row for row in response.json()}
    assert response.status_code == 200
    assert by_id[checked]["risk_flags"] == [{"code": "linked_customers", "message": "Linked to 3 other customers."}]
    assert by_id[unchecked]["risk_flags"] is None  # never checked, and the screen says so


async def test_the_pending_list_needs_a_reviewer(client, refunds):
    response = await client.get("/refunds/pending")

    assert response.status_code == 422


async def test_the_pending_list_never_shows_another_merchants_requests(client, refunds, pool):
    await request(pool, GLOBEX, "A100", 12000)

    response = await client.get("/refunds/pending", headers=REVIEWER)  # the client is acme

    assert response.json() == []


async def test_an_approved_request_leaves_the_pending_list(client, refunds, pool):
    refund_id = await request(pool, ACME, "A100", 5900)
    await approve(pool, refund_id)

    assert (await client.get("/refunds/pending", headers=REVIEWER)).json() == []
