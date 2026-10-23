"""CachedEmbedder: the second identical question does not call the embeddings API, and Redis trouble never breaks a request.

Uses a real Redis (database 15) and the FakeEmbedder, whose `calls` list shows how many times the 'API' was really asked.
"""

import asyncio
import contextlib
import time

import pytest
from helpers import ACME, SANDBOX

from refund_harness.cache.client import create_redis
from refund_harness.cache.embedding_cache import NAMESPACE, CachedEmbedder
from refund_harness.refund_tools import build_registry
from refund_harness.retrieval.embedder import FakeEmbedder
from refund_harness.retrieval.ingest import embed_missing_chunks

QUESTION = "can I send my headphones back?"


@pytest.fixture
def inner() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def cached(inner, redis_client) -> CachedEmbedder:
    return CachedEmbedder(inner, redis_client)


# --- what it saves ---

async def test_repeat_question_is_served_from_cache(cached, inner):
    first = await cached.embed_query(QUESTION)
    second = await cached.embed_query(QUESTION)

    assert len(inner.calls) == 1
    assert second == first  # exactly, number for number


# --- what makes an entry the same entry ---

async def test_other_models_entries_are_never_served(redis_client):
    old, new = FakeEmbedder(), FakeEmbedder()
    new.identity = "fake-hash/768/a-different-prefix"
    await CachedEmbedder(old, redis_client).embed_query(QUESTION)

    await CachedEmbedder(new, redis_client).embed_query(QUESTION)

    assert len(old.calls) == 1 and len(new.calls) == 1  # the newer embedder did not get the older one's vector


async def test_key_hides_the_question_and_expires(cached, redis_client):
    await cached.embed_query(QUESTION)

    [key] = [k.decode() async for k in redis_client.scan_iter("*")]

    assert key.startswith(f"{NAMESPACE}:{cached.identity}:")
    assert "headphones" not in key and QUESTION not in key  # a customer's words are not sitting in Redis as readable keys
    assert 0 < await redis_client.ttl(key) <= 24 * 60 * 60


async def test_wrong_size_value_is_replaced(cached, inner, redis_client):
    await redis_client.set(cached.key(QUESTION), b"not a vector")

    vector = await cached.embed_query(QUESTION)

    assert len(inner.calls) == 1
    assert len(await redis_client.get(cached.key(QUESTION))) == 768 * 8  # overwritten with a good one
    assert len(vector) == 768


# --- failures are never the caller's problem ---

async def test_failed_embedding_is_not_cached(redis_client):
    class Failing(FakeEmbedder):
        async def embed_query(self, text):
            raise RuntimeError("the embeddings API is down")

    cached = CachedEmbedder(Failing(), redis_client)

    with pytest.raises(RuntimeError):
        await cached.embed_query(QUESTION)

    assert [k async for k in redis_client.scan_iter("*")] == []


@contextlib.asynccontextmanager
async def broken_redis(kind: str):
    """A Redis client that refuses connections, or one whose server accepts them and then never answers."""
    server = None
    if kind == "refuses_connections":
        url = "redis://localhost:1/0"
    else:
        async def silent(reader, writer):
            await asyncio.sleep(10)

        server = await asyncio.start_server(silent, "127.0.0.1", 0)
        url = f"redis://127.0.0.1:{server.sockets[0].getsockname()[1]}/0"
    client = create_redis(url, timeout=0.2)
    try:
        yield client
    finally:
        await client.aclose()
        if server:
            server.close()


@pytest.mark.parametrize("kind", ["refuses_connections", "never_answers"])  # the strings are readable ids already
async def test_broken_redis_never_slows_a_question(inner, kind):
    async with broken_redis(kind) as redis:
        started = time.perf_counter()
        vector = await CachedEmbedder(inner, redis).embed_query(QUESTION)
        elapsed = time.perf_counter() - started

    assert len(vector) == 768 and len(inner.calls) == 1
    assert elapsed < 3  # not stuck for the 10 seconds a silent Redis would take


# --- sharing between merchants is safe, because the merchant filter comes AFTER the cache ---

async def test_cache_is_shared_but_policy_stays_per_merchant(pool, sandbox, redis_client):
    await embed_missing_chunks(pool, FakeEmbedder(), merchant_id=SANDBOX)  # the sandbox's own policy, with its secret chunk
    inner = FakeEmbedder()
    cached = CachedEmbedder(inner, redis_client)
    question = {"question": "zebra unicorn rainbow"}  # the words of the sandbox's secret chunk, and of no real merchant's

    sandbox_reply = await build_registry(pool, SANDBOX, cached).get("search_policy").run(question)
    acme_reply = await build_registry(pool, ACME, cached).get("search_policy").run(question)

    assert len(inner.calls) == 1  # the vector was computed once and reused across merchants...
    assert "zebra unicorn rainbow" in sandbox_reply
    assert "zebra" not in acme_reply  # ...yet acme was never shown the sandbox's policy: the search is not cached
