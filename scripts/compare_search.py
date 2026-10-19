"""Keyword search vs semantic search on the same questions, using real Gemini vectors. Prints distances too.

Run from the project root, loading your key from .env (embeds a handful of short questions: a fraction of a cent):
    uv run --env-file .env python scripts/compare_search.py [merchant_id]

Run scripts/ingest_policies.py first so the policy chunks have vectors.
"""

import asyncio
import sys

from refund_harness.adapters.gemini_embeddings import GeminiEmbedder
from refund_harness.data.db import create_pool
from refund_harness.data.repository import (
    search_policy_by_vector,
    search_policy_keyword,
)
from refund_harness.refund_tools import MAX_POLICY_DISTANCE

# (what a customer might say, what we hope it finds)
QUESTIONS = [
    ("send it back", "Return window"),
    ("my headphones broke", "Damaged or defective items"),
    ("money back", "Return window"),
    ("can I return a gift card", "Gift cards"),
    ("who pays for postage", "Shipping costs"),
    ("I changed my mind before it shipped", "Cancellations"),
    ("what is the weather today", "(nothing: unrelated)"),
    ("how do I reset my password", "(nothing: unrelated)"),
]


async def main(merchant_id: str) -> None:
    pool = await create_pool()
    embedder = GeminiEmbedder()
    try:
        print(f"merchant: {merchant_id}   (semantic hits farther than {MAX_POLICY_DISTANCE} are treated as unrelated)\n")
        for question, hoped_for in QUESTIONS:
            keyword = [c["title"] for c in await search_policy_keyword(pool, merchant_id, question)]
            vector = await embedder.embed_query(question)
            semantic = await search_policy_by_vector(pool, merchant_id, vector, limit=3)
            print(f"{question!r}   hoped for: {hoped_for}")
            print(f"  keyword : {keyword or '(no match)'}")
            for c in semantic:
                mark = "" if c["distance"] <= MAX_POLICY_DISTANCE else "   <- too far, would be dropped"
                print(f"  semantic: {c['distance']:.3f}  {c['title']}{mark}")
            print()
    finally:
        await pool.close()


asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "acme"))
