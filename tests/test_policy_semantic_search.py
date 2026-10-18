"""search_policy_by_vector: finds the policy chunks nearest in meaning, scoped to one merchant.

These tests embed the sandbox merchant's chunks with FakeEmbedder (word overlap), so they check the SQL and the
tenant scoping offline. Whether the REAL Gemini vectors capture meaning is what scripts/compare_search.py shows.
"""

import pytest
from helpers import ACME, SANDBOX_CHUNKS

from refund_harness.embedder import Document, FakeEmbedder
from refund_harness.ingest import embed_missing_chunks
from refund_harness.repository import search_policy_by_vector


@pytest.fixture
async def embedder(pool, sandbox):
    """The sandbox chunks, embedded; returns the embedder so a test can embed its question the same way."""
    embedder = FakeEmbedder()
    await embed_missing_chunks(pool, embedder, merchant_id=sandbox)
    return embedder


async def search(pool, embedder, merchant_id: str, question: str, limit: int = 3) -> list[dict]:
    return await search_policy_by_vector(pool, merchant_id, await embedder.embed_query(question), limit)


async def test_the_closest_chunk_comes_first(pool, sandbox, embedder):
    results = await search(pool, embedder, sandbox, "are gift cards final sale")

    assert results[0]["title"] == "Gift cards"


async def test_results_are_ordered_from_closest_to_furthest(pool, sandbox, embedder):
    results = await search(pool, embedder, sandbox, "returned within 30 days", limit=4)

    distances = [r["distance"] for r in results]
    assert distances == sorted(distances)
    assert results[0]["title"] == "Return window"


async def test_an_identical_text_has_distance_zero(pool, sandbox, embedder):
    title, body = SANDBOX_CHUNKS[1]
    [vector] = await embedder.embed_documents([Document(text=body, title=title)])

    [closest, *_] = await search_policy_by_vector(pool, sandbox, vector, limit=1)

    assert closest["title"] == "Gift cards"
    assert closest["distance"] == pytest.approx(0, abs=1e-6)


async def test_limit_caps_the_number_of_results(pool, sandbox, embedder):
    assert len(await search(pool, embedder, sandbox, "returned", limit=2)) == 2


async def test_chunks_without_a_vector_are_not_returned(pool, sandbox):
    # the sandbox fixture alone leaves every chunk un-embedded
    assert await search(pool, FakeEmbedder(), sandbox, "gift cards") == []


async def test_a_merchant_only_gets_its_own_chunks(pool, sandbox, embedder):
    results = await search(pool, embedder, ACME, "zebra unicorn rainbow", limit=50)

    assert all("zebra" not in r["body"] for r in results)  # the sandbox's chunk must not appear for acme
