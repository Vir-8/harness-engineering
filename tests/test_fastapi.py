import pytest

from refund_harness.app import app, get_llm
from refund_harness.errors import ModelCallError, ModelTimeoutError, ReplyParseError
from refund_harness.llm import FakeLLM, RaisingLLM
from refund_harness.types import LLMResponse, ToolCall


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


async def test_missing_merchant_header_returns_422(client):
    fake = FakeLLM([LLMResponse(text="should never be reached")])
    app.dependency_overrides[get_llm] = lambda: fake
    del client.headers["X-Merchant-Id"]  # this client sends it by default; remove it for this test

    r = await client.post("/refund-agent/ask", json={"question": "Where is order A100?"})

    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["header", "x-merchant-id"]
    assert fake.calls == []  # rejected before the model was ever called


def _order_lookup_script() -> FakeLLM:
    """A scripted model that asks for order A100, then gives a final answer."""
    return FakeLLM([
        LLMResponse(tool_calls=[ToolCall(id="c1", name="get_order", arguments={"order_id": "A100"})]),
        LLMResponse(text="done"),
    ])


@pytest.mark.parametrize("merchant, expected, other", [
    ("acme", "headphones", "blender"),
    ("globex", "blender", "headphones"),
])
async def test_same_question_gets_each_merchants_own_order(client, merchant, expected, other):
    fake = _order_lookup_script()
    app.dependency_overrides[get_llm] = lambda: fake

    r = await client.post(
        "/refund-agent/ask",
        json={"question": "Where is order A100?"},
        headers={"X-Merchant-Id": merchant},
    )

    assert r.status_code == 200
    tool_result = fake.calls[1][-1].content  # what the model was shown after its tool call
    assert expected in tool_result
    assert other not in tool_result


async def test_model_cannot_override_the_tenant(client):
    # acme's request, but the "model" tries to look at globex by sneaking in a merchant_id argument
    fake = FakeLLM([
        LLMResponse(tool_calls=[ToolCall(id="c1", name="get_order", arguments={"order_id": "A100", "merchant_id": "globex"})]),
        LLMResponse(text="done"),
    ])
    app.dependency_overrides[get_llm] = lambda: fake

    r = await client.post("/refund-agent/ask", json={"question": "Where is order A100?"})  # default header: acme

    assert r.status_code == 200
    tool_result = fake.calls[1][-1].content
    assert "headphones" in tool_result  # still acme's order
    assert "blender" not in tool_result


async def test_empty_merchant_header_returns_422(client):
    fake = FakeLLM([LLMResponse(text="should never be reached")])
    app.dependency_overrides[get_llm] = lambda: fake

    r = await client.post(
        "/refund-agent/ask",
        json={"question": "Where is order A100?"},
        headers={"X-Merchant-Id": ""},
    )

    assert r.status_code == 422
    assert fake.calls == []
