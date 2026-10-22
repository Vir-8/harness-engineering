"""OpenAIClient: our Messages -> Responses API input, its output -> our LLMResponse, its errors -> our ModelErrors.

A stand-in for the SDK client records each request and returns real SDK Response objects: no network, no cost.
The live behaviour (that OpenAI accepts the replayed encrypted reasoning) was checked by hand against the real API.
"""

import httpx
import openai
import pytest
from openai.types.responses import Response

from refund_harness.adapters.openai_responses import OpenAIClient
from refund_harness.agent.errors import (
    ModelCallError,
    ModelTimeoutError,
    ReplyParseError,
)
from refund_harness.agent.loop import run_agent
from refund_harness.agent.types import Message, ToolSpec

TOOLS = [ToolSpec(name="get_order", description="Look up an order.", parameters={"type": "object", "properties": {"order_id": {"type": "string"}}})]
REASONING = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "gAAAA-secret"}


def function_call(call_id: str, name: str, arguments: str) -> dict:
    return {"type": "function_call", "id": f"fc_{call_id}", "call_id": call_id, "name": name, "arguments": arguments, "status": "completed"}


def message(text: str) -> dict:
    return {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


def response(*output: dict, status: str = "completed", model: str = "gpt-6-luna-2026-09-22", usage: dict | None = None, **extra) -> Response:
    return Response.model_validate({
        "id": "resp_1", "object": "response", "created_at": 0, "model": model, "status": status,
        "output": list(output), "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
        "usage": usage or {"input_tokens": 1000, "input_tokens_details": {"cached_tokens": 400, "cache_write_tokens": 100}, "output_tokens": 100,
                           "output_tokens_details": {"reasoning_tokens": 60}, "total_tokens": 1100},
        **extra,
    })


class FakeSDK:
    """Stands in for AsyncOpenAI: `responses.create` returns (or raises) the next scripted item and keeps the request."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests: list[dict] = []
        self.responses = self

    async def create(self, **request):
        self.requests.append(request)
        result = self.script.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def client_with(*script) -> tuple[OpenAIClient, FakeSDK]:
    sdk = FakeSDK(*script)
    return OpenAIClient(client=sdk), sdk


REQUEST = httpx.Request("POST", "https://api.openai.com/v1/responses")


# --- the request ---

async def test_request_is_stateless_and_keeps_reasoning():
    llm, sdk = client_with(response(message("hi")))

    await llm.complete([Message(role="user", content="hello")], TOOLS)

    [sent] = sdk.requests
    assert sent["model"] == "gpt-6-luna"
    assert sent["reasoning"] == {"effort": "low"}
    assert sent["store"] is False
    assert sent["include"] == ["reasoning.encrypted_content"]
    assert sent["instructions"]
    assert sent["max_output_tokens"] > 0
    assert sent["input"] == [{"role": "user", "content": "hello"}]
    assert sent["tools"] == [{"type": "function", "name": "get_order", "description": "Look up an order.",
                              "parameters": TOOLS[0].parameters, "strict": False}]


# --- the reply ---

async def test_tool_calls_keep_the_providers_ids():
    llm, _ = client_with(response(REASONING, function_call("call_1", "get_order", '{"order_id": "A100"}'), function_call("call_2", "get_order", '{"order_id": "A300"}')))

    reply = await llm.complete([Message(role="user", content="A100 and A300?")], TOOLS)

    assert [(c.id, c.name, c.arguments) for c in reply.tool_calls] == [
        ("call_1", "get_order", {"order_id": "A100"}),
        ("call_2", "get_order", {"order_id": "A300"}),
    ]
    assert reply.text == ""
    assert [item["type"] for item in reply.provider_items] == ["reasoning", "function_call", "function_call"]
    assert reply.provider_items[0]["encrypted_content"] == "gAAAA-secret"


async def test_text_and_refusal_are_both_answers():
    refusal = {"type": "message", "id": "msg_2", "role": "assistant", "status": "completed",
               "content": [{"type": "refusal", "refusal": "I can't help with that."}]}
    llm, _ = client_with(response(message("Your refund is on its way.")), response(refusal))

    assert (await llm.complete([Message(role="user", content="?")], TOOLS)).text == "Your refund is on its way."
    assert (await llm.complete([Message(role="user", content="?")], TOOLS)).text == "I can't help with that."


async def test_cost_uses_the_snapshots_published_price():
    llm, _ = client_with(response(message("ok")))

    usage = (await llm.complete([Message(role="user", content="?")], TOOLS)).usage

    assert (usage.provider, usage.request_model, usage.model) == ("openai", "gpt-6-luna", "gpt-6-luna-2026-09-22")
    assert (usage.input_tokens, usage.cache_read_tokens, usage.cache_write_tokens, usage.output_tokens) == (1000, 400, 100, 100)
    # per million tokens: 500 uncached x $0.10 + 400 cache reads x $0.01 + 100 cache writes x $0.125 + 100 out x $0.50
    assert usage.cost_usd == pytest.approx((500 * 0.10 + 400 * 0.01 + 100 * 0.125 + 100 * 0.50) / 1e6, abs=1e-6)  # rounded to millionths


async def test_unreadable_tool_arguments_are_a_parse_error():
    llm, _ = client_with(response(function_call("call_1", "get_order", '{"order_id": ')))

    with pytest.raises(ReplyParseError):
        await llm.complete([Message(role="user", content="?")], TOOLS)


@pytest.mark.parametrize(
    "output, expected_in_error",
    [((REASONING,), "max_output_tokens"), ((message("Yes, order A100 is within the return wi"),), "incomplete")],
    ids=["before_any_answer", "mid_answer"],
)
async def test_cut_off_reply_is_a_failed_call(output, expected_in_error):
    cut_off = response(*output, status="incomplete", incomplete_details={"reason": "max_output_tokens"})
    llm, _ = client_with(cut_off)

    with pytest.raises(ModelCallError, match=expected_in_error):
        await llm.complete([Message(role="user", content="?")], TOOLS)


# --- failures ---

@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (openai.APITimeoutError(request=REQUEST), ModelTimeoutError),
        (openai.RateLimitError("slow down", response=httpx.Response(429, request=REQUEST), body=None), ModelCallError),
        (openai.AuthenticationError("bad key", response=httpx.Response(401, request=REQUEST), body=None), ModelCallError),
        (openai.APIConnectionError(request=REQUEST), ModelCallError),
    ],
    ids=["timeout", "rate_limited", "bad_key", "no_connection"],
)
async def test_sdk_failures_become_our_model_errors(raised, expected):
    llm, _ = client_with(raised)

    with pytest.raises(expected):
        await llm.complete([Message(role="user", content="?")], TOOLS)


async def test_missing_key_fails_the_call_not_startup(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    llm = OpenAIClient()  # nothing happens yet

    with pytest.raises(ModelCallError, match="OpenAIError"):
        await llm.complete([Message(role="user", content="?")], TOOLS)


# --- structured replies (for callers such as the eval judge) ---

async def test_structured_reply_follows_the_schema():
    schema = {"type": "object", "properties": {"verdict": {"type": "string"}}}
    llm, sdk = client_with(response(message('{"verdict": "supported"}')))

    result, usage = await llm.complete_structured("grade this", schema)

    assert result == {"verdict": "supported"}
    assert usage.cost_usd is not None
    assert sdk.requests[0]["text"]["format"]["schema"] == schema


# --- in the agent loop ---

async def test_next_call_gets_reasoning_and_tool_result(registry):
    llm, sdk = client_with(
        response(REASONING, function_call("call_1", "get_order", '{"order_id": "A100"}')),
        response(message("A100 was delivered 3 days ago.")),
    )

    result = await run_agent(llm, registry, "Where is A100?")

    assert result.final_text == "A100 was delivered 3 days ago."
    second_input = sdk.requests[1]["input"]
    assert REASONING in second_input
    assert {"type": "function_call_output", "call_id": "call_1", "output": "delivered 3 days ago"} in second_input
