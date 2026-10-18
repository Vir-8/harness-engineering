"""Policy search over each merchant's own policy chunks.

search_policy_keyword matches WORDS (Postgres full-text search: lowercased, stemmed, filler words dropped).
Seed data (db/init/06): acme's return window is 30 days, globex's is 14. Both have a chunk titled "Return window".
"""

import pytest
from helpers import ACME, GLOBEX

from refund_harness.repository import search_policy_keyword


async def titles(pool, merchant_id: str, query: str, limit: int = 3) -> list[str]:
    return [chunk["title"] for chunk in await search_policy_keyword(pool, merchant_id, query, limit)]


# --- what keyword search finds ---

async def test_a_word_in_the_policy_finds_its_chunk(pool):
    assert await titles(pool, ACME, "cancelled") == ["Cancellations"]


async def test_different_forms_of_a_word_match_thanks_to_stemming(pool):
    # the policy says "returned"; the customer asks about "returns"
    assert "Return window" in await titles(pool, ACME, "returns")


async def test_each_result_carries_the_title_and_the_text(pool):
    chunk = (await search_policy_keyword(pool, ACME, "cancelled"))[0]

    assert chunk["title"] == "Cancellations"
    assert "has not shipped" in chunk["body"]


async def test_a_chunk_that_mentions_the_word_more_ranks_first(pool):
    # "Shipping costs" says shipping twice (title and text); other chunks mention it once
    assert (await titles(pool, ACME, "shipping"))[0] == "Shipping costs"


async def test_limit_caps_the_number_of_results(pool):
    assert len(await titles(pool, ACME, "refund", limit=2)) == 2


# --- what keyword search cannot find: same meaning, different words ---

@pytest.mark.parametrize("customer_wording", ["send it back", "my headphones broke", "money back"])
async def test_keyword_search_misses_questions_that_use_other_words(pool, customer_wording):
    # The policy says "returned", "damaged or defective" and "refund". This gap is what semantic search is for.
    assert await titles(pool, ACME, customer_wording) == []


# --- tenant isolation ---

async def test_each_merchant_only_finds_its_own_policy(pool):
    assert await titles(pool, GLOBEX, "14 days") == ["Return window"]
    assert await titles(pool, ACME, "14 days") == []  # acme's window is 30 days


async def test_a_merchant_never_sees_another_merchants_chunks(pool):
    for merchant in (ACME, GLOBEX):
        for chunk in await search_policy_keyword(pool, merchant, "refund OR return OR gift", limit=50):
            owner = await owner_of(pool, chunk["title"], chunk["body"])
            assert owner == merchant


async def owner_of(pool, title: str, body: str) -> str:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT merchant_id FROM policy_chunks WHERE title = %s AND body = %s", (title, body))
        return (await cur.fetchone())["merchant_id"]


# --- text from a model can be anything ---

@pytest.mark.parametrize(
    "odd_text",
    ["", "   ", "!!!", '"unbalanced quote', "'; DROP TABLE orders; --"],
    ids=["empty", "blank", "punctuation", "open_quote", "sql_injection_attempt"],
)
async def test_odd_search_text_returns_nothing_instead_of_failing(pool, odd_text):
    assert await search_policy_keyword(pool, ACME, odd_text) == []
