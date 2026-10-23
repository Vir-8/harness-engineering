"""embed_missing_chunks: fills in vectors for policy chunks that don't have one yet.

Every test works on the throwaway `sandbox` merchant (see conftest), so the seed policies (and any real embeddings you paid for) are never touched.
"""

import json

import psycopg.errors
import pytest
from helpers import SANDBOX_CHUNKS

from refund_harness.retrieval.embedder import Document, FakeEmbedder
from refund_harness.retrieval.ingest import embed_missing_chunks


class OneBadVectorEmbedder:
    """Good vectors, except the second one has the wrong size, so the database refuses that row's UPDATE."""

    dimensions = 768

    async def embed_documents(self, documents):
        vectors = await FakeEmbedder().embed_documents(documents)
        vectors[1] = [0.5, 0.5, 0.5]
        return vectors


async def stored(pool, merchant_id: str) -> list[dict]:
    """title, whether it has a vector, its size and its numbers, in insertion order."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT title, embedding IS NOT NULL AS embedded, vector_dims(embedding) AS dims, embedding::text AS numbers "
            "FROM policy_chunks WHERE merchant_id = %s ORDER BY id",
            (merchant_id,),
        )
        return await cur.fetchall()


async def test_every_missing_chunk_gets_a_vector(pool, sandbox):
    fake = FakeEmbedder()

    count = await embed_missing_chunks(pool, fake, merchant_id=sandbox)

    rows = await stored(pool, sandbox)
    assert count == len(SANDBOX_CHUNKS)
    assert {row["dims"] for row in rows} == {768}
    for (title, body), row in zip(SANDBOX_CHUNKS, rows, strict=True):
        [expected] = await fake.embed_documents([Document(text=body, title=title)])
        assert json.loads(row["numbers"]) == pytest.approx(expected, abs=1e-6)  # its own, not a neighbour's (pgvector stores 32-bit floats)


async def test_later_runs_embed_only_whats_missing(pool, sandbox):
    fake = FakeEmbedder()
    await embed_missing_chunks(pool, fake, merchant_id=sandbox)
    calls_after_first_run = len(fake.calls)

    assert await embed_missing_chunks(pool, fake, merchant_id=sandbox) == 0
    assert len(fake.calls) == calls_after_first_run  # nothing missing: not even an empty call

    async with pool.connection() as conn:
        await conn.execute("INSERT INTO policy_chunks (merchant_id, title, body) VALUES (%s, 'Warranty', 'One year warranty.')", (sandbox,))

    assert await embed_missing_chunks(pool, fake, merchant_id=sandbox) == 1
    assert len(fake.calls) == calls_after_first_run + 1


async def test_failed_write_keeps_no_vectors(pool, sandbox):
    with pytest.raises(psycopg.errors.DataException):  # a 3-number vector in a vector(768) column
        await embed_missing_chunks(pool, OneBadVectorEmbedder(), merchant_id=sandbox)

    assert not any(row["embedded"] for row in await stored(pool, sandbox))  # the first chunk's good write was undone too
