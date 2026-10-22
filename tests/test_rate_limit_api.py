"""The limit on POST /refund-agent/ask, through the real app: 429 before any model is called, one merchant at a time."""

import pytest
from helpers import ACME

from refund_harness.agent.llm import FakeLLM
from refund_harness.agent.types import LLMResponse
from refund_harness.app import app, get_llm, get_rate_limiter
from refund_harness.cache.client import create_redis
from refund_harness.cache.rate_limit import RateLimiter

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


async def test_requests_over_the_limit_get_a_429_with_retry_after(client, limited, fake_model):
    statuses = [(await ask(client)).status_code for _ in range(4)]

    assert statuses == [200, 200, 200, 429]


async def test_the_refusal_says_when_to_retry_and_reveals_nothing_internal(client, limited, fake_model):
    for _ in range(3):
        await ask(client)

    refused = await ask(client)

    assert 1 <= int(refused.headers["Retry-After"]) <= 60
    assert refused.json() == {"detail": "Too many requests. Please wait a moment and try again."}


async def test_a_refused_request_never_reaches_the_model(client, limited, fake_model):
    for _ in range(3):
        await ask(client)
    calls_before = len(fake_model.calls)

    await ask(client)

    assert len(fake_model.calls) == calls_before  # no tokens were spent on it


async def test_allowed_requests_say_how_many_are_left(client, limited, fake_model):
    first = await ask(client)

    assert first.headers["X-RateLimit-Limit"] == "3" and first.headers["X-RateLimit-Remaining"] == "2"


async def test_one_merchant_hitting_the_limit_does_not_block_another(client, limited, fake_model):
    for _ in range(4):
        await ask(client, "acme")

    assert (await ask(client, "globex")).status_code == 200


async def test_other_endpoints_are_not_counted_against_the_ask_limit(client, limited, fake_model):
    for _ in range(3):
        await ask(client)

    assert (await client.get("/health")).status_code == 200
    assert (await client.get("/refunds/pending", headers={"X-Reviewer-Id": "r1"})).status_code == 200


async def test_when_redis_is_down_the_endpoint_still_works_with_no_limit_applied(client, fake_model):
    dead = create_redis("redis://localhost:1/0", timeout=0.2)
    app.dependency_overrides[get_rate_limiter] = lambda: RateLimiter(dead, limit=1, window_s=60)

    statuses = [(await ask(client)).status_code for _ in range(3)]
    await dead.aclose()

    assert statuses == [200, 200, 200]  # fail open: nothing limited while Redis is away, but nothing broken either


async def test_without_redis_configured_there_is_simply_no_limit(client, fake_model):
    assert [(await ask(client)).status_code for _ in range(5)] == [200] * 5


async def test_a_merchant_id_that_is_absurdly_long_is_refused_before_it_can_become_a_redis_key(client, fake_model):
    response = await client.post("/refund-agent/ask", json=ASK, headers={"X-Merchant-Id": "m" * 65})

    assert response.status_code == 422
