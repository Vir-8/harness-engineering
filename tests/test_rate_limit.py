"""Rate limiting: RateLimiter (at most N requests per window per subject, counted atomically in a real Redis, database 15),
and the limit on POST /refund-agent/ask through the real app: 429 before any model is called, one merchant at a time.
"""

import asyncio
import time

import pytest
from helpers import ACME

from refund_harness.agent.llm import FakeLLM
from refund_harness.agent.types import LLMResponse
from refund_harness.app import app, get_llm, get_rate_limiter
from refund_harness.cache.client import create_redis
from refund_harness.cache.rate_limit import RateLimiter


@pytest.fixture
def limiter(redis_client) -> RateLimiter:
    return RateLimiter(redis_client, limit=3, window_s=60)


def unreachable_redis():
    return create_redis("redis://localhost:1/0", timeout=0.2)


# --- the limiter ---

async def test_limit_is_enforced_with_a_wait_time(limiter):
    results = [await limiter.check("ask", "acme") for _ in range(4)]

    assert [r.allowed for r in results] == [True, True, True, False]
    assert [r.remaining for r in results] == [2, 1, 0, 0]
    assert 1 <= results[-1].retry_after_s <= 60


@pytest.mark.parametrize(
    "scope, subject", [("ask", "globex"), ("approve", "acme")], ids=["another_subject", "another_scope"]
)
async def test_each_subject_has_its_own_allowance(limiter, scope, subject):
    for _ in range(4):
        await limiter.check("ask", "acme")

    assert (await limiter.check(scope, subject)).allowed  # acme being over its ask limit says nothing about the others


async def test_count_resets_with_the_window(redis_client):
    limiter = RateLimiter(redis_client, limit=2, window_s=1)
    assert [r.allowed for r in [await limiter.check("ask", "acme") for _ in range(3)]] == [True, True, False]

    await asyncio.sleep(1.2)

    assert (await limiter.check("ask", "acme")).allowed


async def test_concurrent_requests_respect_the_limit(redis_client):
    limiter = RateLimiter(redis_client, limit=10, window_s=60)

    results = await asyncio.gather(*(limiter.check("ask", "acme") for _ in range(50)))

    assert sum(r.allowed for r in results) == 10  # a read-then-write counter would let several extra slip through


@pytest.mark.parametrize("found_without_expiry", [False, True], ids=["new_counter", "counter_found_without_an_expiry"])
async def test_counter_always_expires(limiter, redis_client, found_without_expiry):
    key = limiter.key("ask", "acme")
    if found_without_expiry:
        await redis_client.set(key, 1)  # no expiry: as if something else had created it
        assert await redis_client.ttl(key) == -1

    await limiter.check("ask", "acme")

    assert 0 < await redis_client.ttl(key) <= 60


async def test_refusals_do_not_extend_the_window(redis_client):
    limiter = RateLimiter(redis_client, limit=1, window_s=60)
    key = limiter.key("ask", "acme")
    await limiter.check("ask", "acme")
    before = await redis_client.pttl(key)  # milliseconds: whole seconds could not show a reset from 60 s to 60 s

    await asyncio.sleep(0.15)
    for _ in range(5):
        await limiter.check("ask", "acme")  # hammering while limited

    assert await redis_client.pttl(key) < before - 100  # the window kept running down; it was not restarted


async def test_unreachable_redis_lets_requests_through():
    dead = unreachable_redis()
    limiter = RateLimiter(dead, limit=3, window_s=60)
    started = time.perf_counter()

    result = await limiter.check("ask", "acme")
    await dead.aclose()

    assert result.allowed and result.degraded
    assert time.perf_counter() - started < 2


# --- the limit on POST /refund-agent/ask ---

ASK = {"question": "Can I return order A100?"}


@pytest.fixture
def fake_model():
    fake = FakeLLM([LLMResponse(text="Yes.")] * 50)
    app.dependency_overrides[get_llm] = lambda: fake
    return fake


@pytest.fixture
def limited(redis_client):
    """The app with a limit of 3 per minute, on the test Redis."""
    app.dependency_overrides[get_rate_limiter] = lambda: RateLimiter(redis_client, limit=3, window_s=60)


async def ask(client, merchant: str = ACME):
    return await client.post("/refund-agent/ask", json=ASK, headers={"X-Merchant-Id": merchant})


async def test_over_the_limit_gets_429_before_the_model(client, limited, fake_model):
    statuses = [(await ask(client)).status_code for _ in range(3)]
    calls_before = len(fake_model.calls)

    refused = await ask(client)

    assert statuses + [refused.status_code] == [200, 200, 200, 429]
    assert len(fake_model.calls) == calls_before  # no tokens were spent on it


async def test_responses_say_whats_left_and_when_to_retry(
    client, limited, fake_model
):
    first = await ask(client)
    for _ in range(2):
        await ask(client)

    refused = await ask(client)

    assert first.headers["X-RateLimit-Limit"] == "3" and first.headers["X-RateLimit-Remaining"] == "2"
    assert 1 <= int(refused.headers["Retry-After"]) <= 60
    assert refused.json() == {"detail": "Too many requests. Please wait a moment and try again."}


async def test_one_merchant_cannot_block_another(client, limited, fake_model):
    for _ in range(4):
        await ask(client, "acme")

    assert (await ask(client, "globex")).status_code == 200


async def test_endpoint_works_without_redis(client, fake_model):
    dead = unreachable_redis()
    app.dependency_overrides[get_rate_limiter] = lambda: RateLimiter(dead, limit=1, window_s=60)

    statuses = [(await ask(client)).status_code for _ in range(3)]
    await dead.aclose()

    assert statuses == [200, 200, 200]  # fail open: nothing limited while Redis is away, but nothing broken either
