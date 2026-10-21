"""The customer, card and address data the graph is built from (db/init/08).

The seed has a deliberate shape: ana, ben and cy share one card; ben and eve share an address; eve and fay share
another card; dee has no links at all; and globex has a customer reusing acme's card fingerprint and address text.
"""

import pytest
from psycopg.errors import ForeignKeyViolation, GeneratedAlways


async def column(pool, sql: str, *params) -> list:
    async with pool.connection() as conn:
        cur = await conn.execute(sql, params)
        return [next(iter(row.values())) for row in await cur.fetchall()]


async def customers_sharing(pool, merchant: str, column_name: str, value) -> list[str]:
    return sorted(await column(
        pool, f"SELECT DISTINCT customer_id FROM orders WHERE merchant_id = %s AND {column_name} = %s", merchant, value
    ))


async def test_three_acme_customers_share_one_card(pool):
    assert await customers_sharing(pool, "acme", "payment_method_id", "pm-1111") == ["ana", "ben", "cy"]


async def test_ben_and_eve_share_an_address_and_eve_and_fay_share_a_card(pool):
    elm = (await column(pool, "SELECT id FROM addresses WHERE merchant_id = 'acme' AND normalized = '12 elm st 94110'"))[0]

    assert await customers_sharing(pool, "acme", "shipping_address_id", elm) == ["ben", "eve"]
    assert await customers_sharing(pool, "acme", "payment_method_id", "pm-3333") == ["eve", "fay"]


async def test_dee_is_connected_to_nobody(pool):
    [card] = await column(pool, "SELECT payment_method_id FROM orders WHERE merchant_id = 'acme' AND customer_id = 'dee'")
    [address] = await column(pool, "SELECT shipping_address_id FROM orders WHERE merchant_id = 'acme' AND customer_id = 'dee'")

    assert await customers_sharing(pool, "acme", "payment_method_id", card) == ["dee"]
    assert await customers_sharing(pool, "acme", "shipping_address_id", address) == ["dee"]


async def test_every_order_has_a_customer_a_card_and_an_address(pool):
    [missing] = await column(pool, "SELECT count(*) FROM orders WHERE customer_id IS NULL OR payment_method_id IS NULL OR shipping_address_id IS NULL")

    assert missing == 0


async def test_globex_reuses_acmes_card_fingerprint_and_address_text_but_they_are_separate_rows(pool):
    """The tenant trap: a graph keyed on the fingerprint or the text alone would wrongly join the two merchants."""
    fingerprints = await column(pool, "SELECT merchant_id FROM payment_methods WHERE fingerprint = 'fp-1111' ORDER BY 1")
    addresses = await column(pool, "SELECT merchant_id FROM addresses WHERE normalized = '12 elm st 94110' ORDER BY 1")

    assert fingerprints == ["acme", "globex"] and addresses == ["acme", "globex"]


async def test_address_ids_are_generated_by_the_database_not_chosen(pool):
    async with pool.connection() as conn:
        with pytest.raises(GeneratedAlways):
            await conn.execute("INSERT INTO addresses (merchant_id, id, normalized, display) VALUES ('acme', 999, 'x', 'x')")


async def test_writing_the_same_address_twice_finds_the_existing_row(pool):
    find_or_create = (
        "INSERT INTO addresses (merchant_id, normalized, display) VALUES ('acme', %s, %s) "
        "ON CONFLICT (merchant_id, normalized) DO UPDATE SET display = addresses.display RETURNING id"
    )
    async with pool.connection() as conn:
        async with conn.transaction(force_rollback=True):  # nothing is kept
            first = (await (await conn.execute(find_or_create, ("5 test way 94110", "5 Test Way"))).fetchone())["id"]
            again = (await (await conn.execute(find_or_create, ("5 test way 94110", "5 test way."))).fetchone())["id"]

    assert first == again


async def test_an_order_cannot_point_at_another_merchants_customer(pool):
    async with pool.connection() as conn:
        with pytest.raises(ForeignKeyViolation):
            await conn.execute(
                "INSERT INTO orders (merchant_id, id, item, total_cents, status, customer_id) "
                "VALUES ('acme', 'X1', 'thing', 100, 'placed', 'gus')"  # gus is globex's customer
            )
