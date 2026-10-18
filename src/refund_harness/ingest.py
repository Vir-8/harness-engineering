"""Ingestion: embed policy chunks and store the vectors.

An offline job that runs when policy text changes. It is not part of answering a request.
"""

from psycopg_pool import AsyncConnectionPool

from .embedder import Document, Embedder
from .repository import to_vector_literal


async def embed_missing_chunks(pool: AsyncConnectionPool, embedder: Embedder, merchant_id: str | None = None) -> int:
    """Embed every chunk that has no vector yet and store it. Returns how many were embedded.

    merchant_id=None means all merchants (what the real job wants); tests pass one so they never touch seed data.
    Running it again only pays for chunks that are still missing a vector.
    """
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, title, body FROM policy_chunks "
            "WHERE embedding IS NULL AND (%s::text IS NULL OR merchant_id = %s) ORDER BY id",
            (merchant_id, merchant_id),
        )
        chunks = await cur.fetchall()
    if not chunks:
        return 0

    # The network call happens here, with no database connection held: a slow API must not tie up the pool.
    vectors = await embedder.embed_documents([Document(text=c["body"], title=c["title"]) for c in chunks])

    async with pool.connection() as conn:
        async with conn.transaction():  # all vectors are stored, or none
            for chunk, vector in zip(chunks, vectors, strict=True):
                await conn.execute(
                    "UPDATE policy_chunks SET embedding = %s::vector WHERE id = %s",
                    (to_vector_literal(vector), chunk["id"]),
                )
    return len(chunks)
