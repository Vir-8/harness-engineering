"""Calls the real Gemini embedder once and prints what comes back. Costs a fraction of a cent.

Run from the project root, loading your key from .env:
    uv run --env-file .env python scripts/try_embedder.py
"""

import asyncio
import math

from refund_harness.adapters.gemini_embeddings import GeminiEmbedder
from refund_harness.retrieval.embedder import Document


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


async def main() -> None:
    embedder = GeminiEmbedder()
    docs = [
        Document("If an item arrives damaged or defective, we will replace it or refund the full price.", title="Damaged items"),
        Document("Gift cards are final sale and cannot be refunded or exchanged for cash.", title="Gift cards"),
    ]
    doc_vectors = await embedder.embed_documents(docs)
    print(f"{len(doc_vectors)} documents, {len(doc_vectors[0])} dimensions each")

    for question in ["my headphones broke", "can I get my money back for a gift card"]:
        q = await embedder.embed_query(question)
        scores = [round(cosine_similarity(q, d), 3) for d in doc_vectors]
        print(f"{question!r}: damaged/defective={scores[0]}  gift-card={scores[1]}")


asyncio.run(main())
