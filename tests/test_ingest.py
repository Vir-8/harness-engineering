"""embed_missing_chunks: fills in vectors for policy chunks that don't have one yet.

Every test works on the throwaway `sandbox` merchant (see conftest), so the seed policies (and any real embeddings you paid for) are never touched.
"""

import json

import pytest
from helpers import SANDBOX_CHUNKS

from refund_harness.embedder import Document, FakeEmbedder
from refund_harness.ingest import embed_missing_chunks


class BrokenEmbedder:
    dimensions = 768

    async def embed_documents(self, documents):
        raise RuntimeError("the embeddings service is down")


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


async def test_every_chunk_without_a_vector_gets_one_of_the_right_size(pool, sandbox):
    count = await embed_missing_chunks(pool, FakeEmbedder(), merchant_id=sandbox)

    rows = await stored(pool, sandbox)
    assert count == len(SANDBOX_CHUNKS)
    assert all(row["embedded"] for row in rows)
    assert {row["dims"] for row in rows} == {768}


async def test_each_chunk_gets_its_own_vector_not_a_neighbours(pool, sandbox):
    fake = FakeEmbedder()
    await embed_missing_chunks(pool, fake, merchant_id=sandbox)

    for (title, body), row in zip(SANDBOX_CHUNKS, await stored(pool, sandbox), strict=True):
        [expected] = await fake.embed_documents([Document(text=body, title=title)])
        actual = json.loads(row["numbers"])
        assert actual == pytest.approx(expected, abs=1e-6)  # pgvector stores 32-bit floats


async def test_a_second_run_embeds_nothing_and_makes_no_call(pool, sandbox):
    fake = FakeEmbedder()
    await embed_missing_chunks(pool, fake, merchant_id=sandbox)
    calls_after_first_run = len(fake.calls)

    count = await embed_missing_chunks(pool, fake, merchant_id=sandbox)

    assert count == 0
    assert len(fake.calls) == calls_after_first_run


async def test_only_new_chunks_are_embedded_on_a_later_run(pool, sandbox):
    fake = FakeEmbedder()
    await embed_missing_chunks(pool, fake, merchant_id=sandbox)
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO policy_chunks (merchant_id, title, body) VALUES (%s, 'Warranty', 'One year warranty.')", (sandbox,))
    calls_before = len(fake.calls)

    count = await embed_missing_chunks(pool, fake, merchant_id=sandbox)

    assert count == 1
    assert len(fake.calls) - calls_before == 1


async def test_a_failing_embedder_leaves_every_chunk_untouched(pool, sandbox):
    with pytest.raises(RuntimeError):
        await embed_missing_chunks(pool, BrokenEmbedder(), merchant_id=sandbox)

    assert not any(row["embedded"] for row in await stored(pool, sandbox))


async def test_if_a_write_fails_halfway_none_of_the_vectors_are_kept(pool, sandbox):
    with pytest.raises(Exception):  # the database's complaint about a 3-number vector in a vector(768) column
        await embed_missing_chunks(pool, OneBadVectorEmbedder(), merchant_id=sandbox)

    assert not any(row["embedded"] for row in await stored(pool, sandbox))  # the first chunk's good write was undone too


async def test_other_merchants_chunks_are_not_touched(pool, sandbox):
    async def acme_missing() -> int:
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT count(*) AS n FROM policy_chunks WHERE merchant_id = 'acme' AND embedding IS NULL")
            return (await cur.fetchone())["n"]

    before = await acme_missing()

    await embed_missing_chunks(pool, FakeEmbedder(), merchant_id=sandbox)

    assert await acme_missing() == before
