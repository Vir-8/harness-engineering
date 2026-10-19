"""The seam between the harness and any embeddings provider (same idea as agent/llm.py).

An embedding turns text into a vector (a list of numbers) so that texts with similar meaning end up close together.
Providers want to know what the text is FOR, so documents and questions go through different methods.
"""

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Protocol

EMBEDDING_DIMENSIONS = 768  # the size of the `vector(N)` column; every embedder must produce exactly this


@dataclass(frozen=True)
class Document:
    """A piece of text to store and search, e.g. one policy chunk."""

    text: str
    title: str | None = None


class Embedder(Protocol):
    dimensions: int

    async def embed_documents(self, documents: list[Document]) -> list[list[float]]:
        """Embed text that will be stored and searched (policy chunks). One vector per document, same order."""
        ...

    async def embed_query(self, text: str) -> list[float]:
        """Embed a question that will be compared against the stored documents."""
        ...


class FakeEmbedder:
    """Deterministic and offline: no network, no cost. For testing the plumbing.

    It hashes each word to a position in the vector, so texts that SHARE WORDS get close vectors.
    That is word overlap, NOT meaning: it cannot tell that "broke" and "defective" are related.
    """

    def __init__(self, dimensions: int = EMBEDDING_DIMENSIONS):
        self.dimensions = dimensions
        self.calls: list[str] = []  # every text it was asked to embed, like FakeLLM.calls

    async def embed_documents(self, documents: list[Document]) -> list[list[float]]:
        return [self._embed(f"{d.title or ''} {d.text}") for d in documents]

    async def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def _embed(self, text: str) -> list[float]:
        self.calls.append(text)
        words = re.findall(r"[a-z0-9]+", text.lower())
        if not words:
            raise ValueError("cannot embed empty text")
        vector = [0.0] * self.dimensions
        for word in words:
            # hashlib, not hash(): Python's built-in hash changes between runs, which would break determinism
            position = int(hashlib.sha256(word.encode()).hexdigest(), 16) % self.dimensions
            vector[position] += 1.0
        length = math.sqrt(sum(x * x for x in vector))
        return [x / length for x in vector]
