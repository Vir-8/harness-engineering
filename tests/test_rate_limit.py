"""RateLimiter: at most N requests per window per subject, counted atomically in a real Redis (database 15)."""

import asyncio
import time

import pytest

from refund_harness.cache.client import create_redis
from refund_harness.cache.rate_limit import RateLimiter


@pytest.fixture
def limiter(redis_client) -> RateLimiter:
    return RateLimiter(redis_client, limit=3, window_s=60)


async def test_requests_up_to_the_limit_are_allowed_and_the_next_one_is_not(limiter):
    results = [await limiter.check("ask", "acme") for _ in range(4)]

    assert [r.allowed for r in results] == [True, True, True, False]
    assert [r.remaining for r in results] == [2, 1, 0, 0]


async def test_a_refusal_says_how_long_to_wait(limiter):
    for _ in range(3):
        await limiter.check("ask", "acme")

    refused = await limiter.check("ask", "acme")

    assert not refused.allowed and 1 <= refused.retry_after_s <= 60


async def test_each_subject_has_its_own_allowance(limiter):
    for _ in range(4):
        await limiter.check("ask", "acme")

    assert (await limiter.check("ask", "globex")).allowed  # acme being over the limit says nothing about globex


async def test_each_scope_has_its_own_allowance(limiter):
    for _ in range(4):
        await limiter.check("ask", "acme")

    assert (await limiter.check("approve", "acme")).allowed


async def test_the_count_starts_again_when_the_window_ends(redis_client):
    limiter = RateLimiter(redis_client, limit=2, window_s=1)
    assert [r.allowed for r in [await limiter.check("ask", "acme") for _ in range(3)]] == [True, True, False]

    await asyncio.sleep(1.2)

    assert (await limiter.check("ask", "acme")).allowed


async def test_fifty_simultaneous_requests_let_exactly_the_limit_through(redis_client):
    limiter = RateLimiter(redis_client, limit=10, window_s=60)

    results = await asyncio.gather(*(limiter.check("ask", "acme") for _ in range(50)))

    assert sum(r.allowed for r in results) == 10  # a read-then-write counter would let several extra slip through


async def test_the_counter_always_has_an_expiry(limiter, redis_client):
    await limiter.check("ask", "acme")

    assert 0 < await redis_client.ttl(limiter.key("ask", "acme")) <= 60


async def test_a_counter_found_without_an_expiry_is_given_one_instead_of_blocking_for_ever(limiter, redis_client):
    await redis_client.set(limiter.key("ask", "acme"), 1)  # no expiry: as if something else had created it
    assert await redis_client.ttl(limiter.key("ask", "acme")) == -1

    await limiter.check("ask", "acme")

    assert 0 < await redis_client.ttl(limiter.key("ask", "acme")) <= 60


async def test_refused_requests_do_not_push_the_window_further_away(redis_client):
    limiter = RateLimiter(redis_client, limit=1, window_s=60)
    key = limiter.key("ask", "acme")
    await limiter.check("ask", "acme")
    before = await redis_client.pttl(key)  # milliseconds: whole seconds could not show a reset from 60 s to 60 s

    await asyncio.sleep(0.15)
    for _ in range(5):
        await limiter.check("ask", "acme")  # hammering while limited

    assert await redis_client.pttl(key) < before - 100  # the window kept running down; it was not restarted


async def test_when_redis_is_unreachable_requests_are_let_through_and_marked_degraded():
    dead = create_redis("redis://localhost:1/0", timeout=0.2)
    limiter = RateLimiter(dead, limit=3, window_s=60)
    started = time.perf_counter()

    result = await limiter.check("ask", "acme")
    await dead.aclose()

    assert result.allowed and result.degraded
    assert time.perf_counter() - started < 2


@pytest.mark.parametrize("limit, window", [(0, 60), (3, 0), (-1, 60)])
def test_a_limit_or_window_below_one_is_refused(redis_client, limit, window):
    with pytest.raises(ValueError):
        RateLimiter(redis_client, limit=limit, window_s=window)
