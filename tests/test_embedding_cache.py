"""CachedEmbedder: the second identical question does not call the embeddings API, and Redis trouble never breaks a request.

Uses a real Redis (database 15) and the FakeEmbedder, whose `calls` list shows how many times the 'API' was really asked.
"""

import asyncio
import time

import pytest
from helpers import ACME, SANDBOX
from opentelemetry import trace

from refund_harness.cache.client import create_redis
from refund_harness.cache.embedding_cache import NAMESPACE, CachedEmbedder
from refund_harness.refund_tools import build_registry
from refund_harness.retrieval.embedder import Document, FakeEmbedder
from refund_harness.retrieval.ingest import embed_missing_chunks

QUESTION = "can I send my headphones back?"


@pytest.fixture
def inner() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def cached(inner, redis_client) -> CachedEmbedder:
    return CachedEmbedder(inner, redis_client)


# --- what it saves ---

async def test_the_second_identical_question_does_not_reach_the_embeddings_api(cached, inner):
    first = await cached.embed_query(QUESTION)
    second = await cached.embed_query(QUESTION)

    assert len(inner.calls) == 1
    assert (cached.stats.hits, cached.stats.misses) == (1, 1)
    assert second == first  # exactly, number for number


async def test_a_different_question_is_computed_separately(cached, inner):
    await cached.embed_query(QUESTION)
    await cached.embed_query("who pays for postage?")

    assert len(inner.calls) == 2


async def test_the_question_is_not_normalised_so_different_spelling_is_a_different_entry(cached, inner):
    await cached.embed_query("Can I return it?")
    await cached.embed_query("can i return it?")

    assert len(inner.calls) == 2  # the model might embed them differently: we do not assume it does not


async def test_documents_are_never_cached(cached, inner):
    document = Document(text="Items may be returned within 30 days.", title="Return window")

    await cached.embed_documents([document])
    await cached.embed_documents([document])

    assert len(inner.calls) == 2


# --- what makes an entry the same entry ---

async def test_entries_of_a_different_model_or_format_are_never_served(redis_client):
    old, new = FakeEmbedder(), FakeEmbedder()
    new.identity = "fake-hash/768/a-different-prefix"
    await CachedEmbedder(old, redis_client).embed_query(QUESTION)

    await CachedEmbedder(new, redis_client).embed_query(QUESTION)

    assert len(old.calls) == 1 and len(new.calls) == 1  # the newer embedder did not get the older one's vector


async def test_the_key_names_the_identity_and_hides_the_question(cached, redis_client):
    await cached.embed_query(QUESTION)

    [key] = [k.decode() async for k in redis_client.scan_iter("*")]

    assert key.startswith(f"{NAMESPACE}:{cached.identity}:")
    assert "headphones" not in key and QUESTION not in key  # a customer's words are not sitting in Redis as readable keys


async def test_every_entry_expires_by_itself(cached, redis_client):
    await cached.embed_query(QUESTION)

    ttl = await redis_client.ttl(cached.key(QUESTION))

    assert 0 < ttl <= 24 * 60 * 60


async def test_the_stored_value_is_the_exact_vector_in_float64_bytes(cached, redis_client):
    vector = await cached.embed_query(QUESTION)

    assert len(await redis_client.get(cached.key(QUESTION))) == 768 * 8 and len(vector) == 768


async def test_a_stored_value_of_the_wrong_size_is_ignored_and_replaced(cached, inner, redis_client):
    await redis_client.set(cached.key(QUESTION), b"not a vector")

    vector = await cached.embed_query(QUESTION)

    assert len(inner.calls) == 1 and cached.stats.errors == 1
    assert len(await redis_client.get(cached.key(QUESTION))) == 768 * 8  # overwritten with a good one
    assert len(vector) == 768


# --- failures are never the caller's problem ---

async def test_a_failed_embeddings_call_is_not_cached_and_still_fails_loudly(redis_client):
    class Failing(FakeEmbedder):
        async def embed_query(self, text):
            raise RuntimeError("the embeddings API is down")

    cached = CachedEmbedder(Failing(), redis_client)

    with pytest.raises(RuntimeError):
        await cached.embed_query(QUESTION)

    assert [k async for k in redis_client.scan_iter("*")] == []


async def test_with_redis_unreachable_the_question_is_still_answered_quickly(inner):
    dead = create_redis("redis://localhost:1/0", timeout=0.2)
    cached = CachedEmbedder(inner, dead)
    started = time.perf_counter()

    vector = await cached.embed_query(QUESTION)
    await dead.aclose()

    assert len(vector) == 768 and len(inner.calls) == 1
    assert cached.stats.errors == 2 and time.perf_counter() - started < 2  # one failed read, one failed write


async def test_with_redis_accepting_connections_but_never_answering_it_times_out_and_carries_on(inner):
    async def silent(reader, writer):
        await asyncio.sleep(10)

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    hung = create_redis(f"redis://127.0.0.1:{port}/0", timeout=0.2)
    cached = CachedEmbedder(inner, hung)
    started = time.perf_counter()
    try:
        vector = await cached.embed_query(QUESTION)
    finally:
        await hung.aclose()
        server.close()

    assert len(vector) == 768 and time.perf_counter() - started < 3  # not stuck for the 10 seconds Redis would take


# --- visibility ---

async def test_the_trace_says_whether_the_cache_was_used(cached):
    with trace.get_tracer("test").start_as_current_span("ask") as span:
        await cached.embed_query(QUESTION)
        assert span.attributes["app.embedding_cache"] == "miss"
        await cached.embed_query(QUESTION)
        assert span.attributes["app.embedding_cache"] == "hit"


async def test_the_trace_says_when_the_cache_was_skipped_because_redis_failed(inner):
    dead = create_redis("redis://localhost:1/0", timeout=0.2)
    cached = CachedEmbedder(inner, dead)
    with trace.get_tracer("test").start_as_current_span("ask") as span:
        await cached.embed_query(QUESTION)
        assert span.attributes["app.embedding_cache"] == "error"
    await dead.aclose()


# --- sharing between merchants is safe, because the merchant filter comes AFTER the cache ---

async def test_one_merchants_cached_question_serves_another_but_each_still_gets_its_own_policy(pool, sandbox, redis_client):
    await embed_missing_chunks(pool, FakeEmbedder(), merchant_id=SANDBOX)  # the sandbox's own policy, with its secret chunk
    inner = FakeEmbedder()
    cached = CachedEmbedder(inner, redis_client)
    question = {"question": "zebra unicorn rainbow"}  # the words of the sandbox's secret chunk, and of no real merchant's

    sandbox_reply = await build_registry(pool, SANDBOX, cached).get("search_policy").run(question)
    acme_reply = await build_registry(pool, ACME, cached).get("search_policy").run(question)

    assert len(inner.calls) == 1  # the vector was computed once and reused across merchants...
    assert "zebra unicorn rainbow" in sandbox_reply
    assert "zebra" not in acme_reply  # ...yet acme was never shown the sandbox's policy: the search is not cached
