"""Embeds every policy chunk that has no vector yet, using the real Gemini embedder.

Run from the project root, loading your key from .env:
    uv run --env-file .env python scripts/ingest_policies.py
"""

import asyncio

from refund_harness.adapters.gemini_embeddings import GeminiEmbedder
from refund_harness.db import create_pool
from refund_harness.ingest import embed_missing_chunks


async def main() -> None:
    pool = await create_pool()
    try:
        count = await embed_missing_chunks(pool, GeminiEmbedder())
    finally:
        await pool.close()
    print(f"embedded {count} chunk(s)")


asyncio.run(main())
