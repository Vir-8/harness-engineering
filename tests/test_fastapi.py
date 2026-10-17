import httpx
import pytest

from refund_harness.app import app, get_llm
from refund_harness.errors import ModelCallError, ModelTimeoutError, ReplyParseError
from refund_harness.llm import FakeLLM, RaisingLLM
from refund_harness.types import LLMResponse, ToolCall


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()      # runs after each test, so fakes can't leak

async def test_ask_success(client):
    app.dependency_overrides[get_llm] = lambda: FakeLLM([LLMResponse(text="Eligible for a refund")])
    r = await client.post("/refund-agent/ask", json={"question": "Can I refund A100?"})
    assert r.status_code == 200
    assert r.json()["answer"] == "Eligible for a refund"

async def test_empty_question_returns_422(client):
    fake = FakeLLM([LLMResponse(text="Eligible for a refund")])
    app.dependency_overrides[get_llm] = lambda: fake
    r = await client.post("/refund-agent/ask", json={"question": ""})

    assert r.status_code == 422
    assert fake.calls == []


async def test_timeout_returns_504(client):
    fake = RaisingLLM(ModelTimeoutError("internal details"))
    app.dependency_overrides[get_llm] = lambda: fake
    r = await client.post("/refund-agent/ask", json={"question": "Test question?"})
    assert r.status_code == 504
    assert "internal" not in r.text


async def test_error_returns_502(client):
    fake = RaisingLLM(ModelCallError("stderr: not logged in"))
    app.dependency_overrides[get_llm] = lambda: fake
    r = await client.post("/refund-agent/ask", json={"question": "Test question?"})
    assert r.status_code == 502
    assert "stderr" not in r.text


async def test_bad_reply_returns_502(client):
    fake = RaisingLLM(ReplyParseError("secret details"))
    app.dependency_overrides[get_llm] = lambda: fake
    r = await client.post("/refund-agent/ask", json={"question": "Test question?"})
    assert r.status_code == 502
    assert "secret" not in r.text


async def test_num_steps_counts_model_turns(client):
    fake = FakeLLM([
        LLMResponse(tool_calls=[ToolCall(id="c1", name="get_order", arguments={"order_id": "A100"})]),
        LLMResponse(text="Order A100 is eligible for a refund"),
    ])
    app.dependency_overrides[get_llm] = lambda: fake
    r = await client.post("/refund-agent/ask", json={"question": "Can I refund A100?"})

    assert r.status_code == 200
    assert r.json() == {"answer": "Order A100 is eligible for a refund", "stop_reason": "final", "num_steps": 2}
    assert "delivered" in fake.calls[1][-1].content  # the real get_order result reached the model
