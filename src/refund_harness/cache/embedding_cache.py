"""Remembers the vector for a question, so asking the same thing twice does not pay for a second embeddings call.

What is cached, and why it is safe to share between merchants: the vector of a QUESTION depends only on the question's
text and on the embedding model. It does not depend on any merchant's data: the merchant filter is applied later, in the
SQL that compares the vector with that merchant's policy chunks, and that search is NOT cached. So the key holds no
merchant, and one merchant's question can serve another's. (Data that does depend on the tenant, such as an order's
status, would need the merchant in its key.)

The key holds everything that determines the vector: the model, the dimensions, how the question is dressed up before
sending (all inside `embedder.identity`) and the text itself. Change any of those and the old entries are simply never
found again, so there is nothing to invalidate. The TTL is only housekeeping.

Redis is optional at runtime: any failure is a cache miss, never an error for the caller.
"""

import hashlib
import logging
from array import array
from dataclasses import dataclass

from opentelemetry import trace
from redis.asyncio import Redis
from redis.exceptions import RedisError

from refund_harness.retrieval.embedder import Document, Embedder

log = logging.getLogger(__name__)

NAMESPACE = "emb:v1"  # bump only if the stored FORMAT below changes
DEFAULT_TTL_S = 24 * 60 * 60


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    errors: int = 0  # Redis failed or held something unusable: the caller was served without the cache


class CachedEmbedder:
    def __init__(self, inner: Embedder, redis: Redis, *, ttl_s: int = DEFAULT_TTL_S):
        self._inner = inner
        self._redis = redis
        self._ttl_s = ttl_s
        self.dimensions = inner.dimensions
        self.identity = inner.identity
        self.stats = CacheStats()

    def key(self, text: str) -> str:
        # The text is hashed, so a customer's question never sits in Redis as a readable key, and keys stay short.
        return f"{NAMESPACE}:{self.identity}:{hashlib.sha256(text.encode()).hexdigest()}"

    async def embed_query(self, text: str) -> list[float]:
        key = self.key(text)
        cached = await self._read(key)
        if cached is not None:
            self.stats.hits += 1
            self._note("hit")
            return cached
        vector = await self._inner.embed_query(text)  # a failure here propagates, and nothing is stored
        stored = await self._write(key, vector)
        self.stats.misses += 1
        self._note("miss" if stored else "error")
        return vector

    async def embed_documents(self, documents: list[Document]) -> list[list[float]]:
        return await self._inner.embed_documents(documents)  # ingestion runs once per chunk: nothing to save

    async def _read(self, key: str) -> list[float] | None:
        try:
            raw = await self._redis.get(key)
        except RedisError as error:
            self._failed("read", error)
            return None
        if raw is None:
            return None
        if len(raw) != self.dimensions * 8:  # not what we wrote (a different format, a truncated value): treat as absent
            self._failed("read", ValueError(f"cached value has {len(raw)} bytes"))
            return None
        vector = array("d")
        vector.frombytes(raw)
        return vector.tolist()

    async def _write(self, key: str, vector: list[float]) -> bool:
        try:
            # float64 bytes: the exact numbers that came back from the model, 6 KB for 768 dimensions
            await self._redis.set(key, array("d", vector).tobytes(), ex=self._ttl_s)
        except RedisError as error:
            self._failed("write", error)
            return False
        return True

    def _failed(self, action: str, error: Exception) -> None:
        self.stats.errors += 1
        log.warning("embedding cache %s failed (%s: %s); continuing without it", action, type(error).__name__, error)

    @staticmethod
    def _note(outcome: str) -> None:
        trace.get_current_span().set_attribute("app.embedding_cache", outcome)  # shows in the trace tree
