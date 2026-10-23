"""Vector search over each merchant's own policy chunks (search_policy_by_vector): nearest in meaning, one merchant only.

These tests embed the sandbox merchant's chunks with FakeEmbedder (word overlap), so they check the SQL and the
tenant scoping offline. Whether the REAL Gemini vectors capture meaning is what the evals' retrieval check measures.
"""

import pytest
from helpers import ACME

from refund_harness.data.repository import search_policy_by_vector
from refund_harness.retrieval.embedder import FakeEmbedder
from refund_harness.retrieval.ingest import embed_missing_chunks


@pytest.fixture
async def embedder(pool, sandbox):
    """The sandbox chunks, embedded; returns the embedder so a test can embed its question the same way."""
    embedder = FakeEmbedder()
    await embed_missing_chunks(pool, embedder, merchant_id=sandbox)
    return embedder


async def search(pool, embedder, merchant_id: str, question: str, limit: int = 3) -> list[dict]:
    return await search_policy_by_vector(pool, merchant_id, await embedder.embed_query(question), limit)


async def test_results_are_closest_first(pool, sandbox, embedder):
    results = await search(pool, embedder, sandbox, "returned within 30 days", limit=4)

    distances = [r["distance"] for r in results]
    assert distances == sorted(distances)
    assert results[0]["title"] == "Return window"


async def test_chunks_without_vectors_are_skipped(pool, sandbox):
    # the sandbox fixture alone leaves every chunk un-embedded
    assert await search(pool, FakeEmbedder(), sandbox, "gift cards") == []


async def test_merchant_gets_only_its_own_chunks(pool, sandbox, embedder):
    results = await search(pool, embedder, ACME, "zebra unicorn rainbow", limit=50)

    assert all("zebra" not in r["body"] for r in results)  # the sandbox's chunk must not appear for acme
