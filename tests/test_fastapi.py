"""POST /refund-agent/ask through the real app: what it refuses, what it answers, and that each merchant sees only its own data.

The model is scripted (FakeLLM); the tools run against the test database.
"""

import pytest
from helpers import ACME, RaisingLLM

from refund_harness.agent.errors import (
    ModelCallError,
    ModelTimeoutError,
    ReplyParseError,
)
from refund_harness.agent.llm import FakeLLM
from refund_harness.agent.types import LLMResponse, ToolCall, Usage
from refund_harness.app import app, get_llm


@pytest.mark.parametrize(
    "question, merchant, wrong_field",
    [
        ("", ACME, ["body", "question"]),
        ("Where is order A100?", None, ["header", "x-merchant-id"]),  # None: no header at all
        ("Where is order A100?", "", ["header", "x-merchant-id"]),
        ("Where is order A100?", "m" * 65, ["header", "x-merchant-id"]),  # before it can become a Redis key
    ],
    ids=["empty_question", "no_merchant_header", "empty_merchant_header", "absurdly_long_merchant_id"],
)
async def test_bad_request_gets_422_before_the_model(client, question, merchant, wrong_field):
    fake = FakeLLM([LLMResponse(text="should never be reached")])
    app.dependency_overrides[get_llm] = lambda: fake
    if merchant is None:
        del client.headers["X-Merchant-Id"]  # this client sends it by default
    else:
        client.headers["X-Merchant-Id"] = merchant

    r = await client.post("/refund-agent/ask", json={"question": question})

    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == wrong_field
    assert fake.calls == []  # no tokens spent on it


@pytest.mark.parametrize(
    "error, status, internal_text",
    [
        (ModelTimeoutError("internal details"), 504, "internal"),
        (ModelCallError("stderr: not logged in"), 502, "stderr"),
        (ReplyParseError("secret details"), 502, "secret"),
    ],
    ids=["timeout", "call_failed", "unusable_reply"],
)
async def test_model_failure_reveals_nothing_internal(client, error, status, internal_text):
    app.dependency_overrides[get_llm] = lambda: RaisingLLM(error)

    r = await client.post("/refund-agent/ask", json={"question": "Test question?"})

    assert r.status_code == status
    assert internal_text not in r.text


async def test_num_steps_counts_model_turns(client):
    fake = FakeLLM([
        LLMResponse(tool_calls=[ToolCall(id="c1", name="get_order", arguments={"order_id": "A100"})]),
        LLMResponse(text="Order A100 is eligible for a refund"),
    ])
    app.dependency_overrides[get_llm] = lambda: fake
    r = await client.post("/refund-agent/ask", json={"question": "Can I refund A100?"})

    assert r.status_code == 200
    body = r.json()
    assert (body["answer"], body["stop_reason"], body["num_steps"]) == ("Order A100 is eligible for a refund", "final", 2)
    assert [(step["kind"], step["name"]) for step in body["steps"]] == [("model", "model"), ("tool", "get_order"), ("model", "model")]
    assert body["steps"][1]["arguments"] == {"order_id": "A100"} and "delivered" in body["steps"][1]["output"]
    assert body["cost_usd"] is None  # the fake model reports no usage, so the cost is unknown, not zero
    assert "delivered" in fake.calls[1][-1].content  # the real get_order result reached the model


async def test_answer_reports_tokens_and_cost(client):
    usage = Usage(model="gpt-6-luna", input_tokens=300, output_tokens=20, cost_usd=0.0001)
    fake = FakeLLM([
        LLMResponse(tool_calls=[ToolCall(id="c1", name="get_order", arguments={"order_id": "A100"})], usage=usage),
        LLMResponse(text="Delivered.", usage=usage),
    ])
    app.dependency_overrides[get_llm] = lambda: fake

    body = (await client.post("/refund-agent/ask", json={"question": "Where is A100?"})).json()

    assert (body["input_tokens"], body["output_tokens"]) == (600, 40)
    assert body["cost_usd"] == pytest.approx(0.0002)
    assert body["steps"][0]["tool_calls"][0]["name"] == "get_order"
    assert body["duration_ms"] > 0


# --- each merchant sees only its own data ---

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
