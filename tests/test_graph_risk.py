"""assess_customer / flags_from: the advisory notes the graph gives about a customer.

The ring (graph-a): ana, ben and cy share a card; ben and eve share an address; eve and fay share another card; dee is alone.
Within 2 customer-steps: ana reaches ben, cy, eve (3); fay reaches eve and ben (2); dee reaches nobody.
"""

import asyncio
import time

import pytest
from graph_helpers import A, B
from neo4j import AsyncGraphDatabase

from refund_harness.graph import risk
from refund_harness.graph.risk import (
    GRAPH_UNAVAILABLE,
    NO_CUSTOMER,
    assess_customer,
    flags_from,
)


@pytest.fixture
async def synced(world):
    await world.sync(A)
    return world


def codes(flags: list[dict]) -> list[str]:
    return [flag["code"] for flag in flags]


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


def test_the_notes_say_how_many_in_words_a_person_can_read():
    [note] = flags_from(3, 0)

    assert "3 other customers" in note["message"] and "2 steps" in note["message"]


# --- against the real graph ---

async def test_a_customer_in_a_ring_is_flagged_with_how_many_are_linked(synced):
    flags = await assess_customer(synced.driver, A, "ana")

    assert codes(flags) == ["linked_customers"]
    assert "3 other customers" in flags[0]["message"]  # ben and cy by card, eve through ben's address


async def test_how_far_the_check_looks_is_two_customer_steps(synced):
    flags = await assess_customer(synced.driver, A, "fay")

    assert "2 other customers" in flags[0]["message"]  # eve (1 step) and ben (2); ana and cy are 3 steps away


async def test_a_customer_connected_to_nobody_checks_out_clean(synced):
    assert await assess_customer(synced.driver, A, "dee") == []  # checked, nothing found: an empty list, not "unavailable"


async def test_refund_requests_among_the_linked_customers_are_noted(synced):
    async with synced.pool.connection() as conn:
        await conn.execute("INSERT INTO refund_requests (merchant_id, order_id, amount_cents) VALUES (%s, 'O2', 1000), (%s, 'O3', 1000)", (A, A))
    await synced.sync(A)

    flags = await assess_customer(synced.driver, A, "ana")  # ben's and cy's requests

    assert codes(flags) == ["linked_customers", "linked_refunds"]
    assert "2 refund requests" in flags[1]["message"]


async def test_a_customers_own_refund_does_not_count_against_them(synced):
    # the seed gives ana's order O1 a refund request; ana's own request is not one of the "linked customers'"
    assert "linked_refunds" not in codes(await assess_customer(synced.driver, A, "ana"))


async def test_another_merchants_identical_ring_does_not_inflate_the_numbers(synced):
    await synced.sync(B)

    flags = await assess_customer(synced.driver, A, "ana")

    assert "3 other customers" in flags[0]["message"]  # not 6: graph-b has the same ids and the same shape


async def test_a_customer_nobody_has_synced_is_checked_clean_not_flagged_as_unavailable(world):
    assert await assess_customer(world.driver, "graph-never-synced", "ana") == []


async def test_an_order_with_no_customer_says_it_cannot_be_checked(synced):
    assert await assess_customer(synced.driver, A, None) == [NO_CUSTOMER]


# --- the check must never break a refund ---

async def test_when_neo4j_is_unreachable_the_answer_is_graph_unavailable_not_an_error(monkeypatch):
    monkeypatch.setattr(risk, "GRAPH_TIMEOUT_S", 0.5)
    dead = AsyncGraphDatabase.driver("bolt://localhost:1", auth=("neo4j", "nothing"))
    started = time.perf_counter()
    try:
        assert await assess_customer(dead, A, "ana") == [GRAPH_UNAVAILABLE]
    finally:
        await dead.close()

    # The driver itself would keep retrying a dead server for about 30 seconds: it is OUR time limit that ends the wait.
    assert time.perf_counter() - started < 5


async def test_a_graph_that_answers_too_slowly_is_given_up_on(monkeypatch):
    class SlowDriver:
        async def execute_query(self, *args, **kwargs):
            await asyncio.sleep(5)

    monkeypatch.setattr(risk, "GRAPH_TIMEOUT_S", 0.05)
    started = time.perf_counter()

    assert await assess_customer(SlowDriver(), A, "ana") == [GRAPH_UNAVAILABLE]
    assert time.perf_counter() - started < 1  # given up on after 50 ms, not after the driver's own 5 seconds


async def test_a_graph_that_raises_is_treated_like_one_that_is_down():
    class BrokenDriver:
        async def execute_query(self, *args, **kwargs):
            raise RuntimeError("the graph exploded")

    assert await assess_customer(BrokenDriver(), A, "ana") == [GRAPH_UNAVAILABLE]


async def test_a_customer_is_never_counted_as_linked_to_themselves_even_when_a_path_loops_back(synced):
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
