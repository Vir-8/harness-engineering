"""read_graph_data: one merchant's rows out of Postgres. Reads the real seed (acme, globex) and changes nothing."""

from helpers import ACME, GLOBEX

from refund_harness.graph.source import read_graph_data


async def test_acmes_data_has_the_seeded_ring(pool):
    data = await read_graph_data(pool, ACME)

    assert [c["id"] for c in data.customers] == ["ana", "ben", "cy", "dee", "eve", "fay"]
    assert [c["id"] for c in data.cards] == ["pm-1111", "pm-2222", "pm-3333"]
    assert len(data.addresses) == 5 and len(data.orders) == 6


async def test_another_merchants_rows_never_appear(pool):
    data = await read_graph_data(pool, ACME)

    assert "gus" not in {c["id"] for c in data.customers}
    assert all(pair["customer_id"] != "gus" for pair in data.customer_cards)


async def test_globex_has_only_its_own_customer(pool):
    data = await read_graph_data(pool, GLOBEX)

    assert [c["id"] for c in data.customers] == ["gus"]
    assert data.customer_cards == [{"customer_id": "gus", "card_id": "pm-1111"}]


async def test_ids_come_out_as_strings_even_where_postgres_has_numbers(pool):
    data = await read_graph_data(pool, ACME)

    assert all(isinstance(a["id"], str) for a in data.addresses)
    assert all(isinstance(pair["address_id"], str) for pair in data.customer_addresses)
    assert all(isinstance(r["id"], str) for r in data.refunds)


async def test_a_customer_card_pair_is_listed_once_however_many_orders_it_has(pool):
    data = await read_graph_data(pool, ACME)
    pairs = [(p["customer_id"], p["card_id"]) for p in data.customer_cards]

    assert len(pairs) == len(set(pairs))


async def test_an_unknown_merchant_has_no_data_and_no_error(pool):
    data = await read_graph_data(pool, "nobody")

    assert not any(vars(data).values())
