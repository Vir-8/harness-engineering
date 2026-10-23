"""Observability: error reports (Sentry), the trace tree printer and tracing setup, and recording content on spans.

For Sentry, a throwaway app raises an error while handling a request whose body holds a secret, and a fake transport
catches what would have been sent, so nothing leaves the machine. Tracing is tested with private providers and
in-memory exporters. The agent loop's own spans are in test_loop.py.
"""

import json
import uuid

import httpx
import pytest
import sentry_sdk
from fastapi import FastAPI
from helpers import FAKE_DSN, CatchingTransport, reset_sentry
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sentry_sdk.integrations.google_genai import GoogleGenAIIntegration
from sentry_sdk.integrations.openai import OpenAIIntegration
from sentry_sdk.integrations.otlp import OTLPIntegration

from refund_harness import observability
from refund_harness.agent.types import LLMResponse, Message, ToolCall, Usage
from refund_harness.data.db import create_pool
from refund_harness.observability import (
    MAX_CONTENT_CHARS,
    TraceTreeExporter,
    enable_tracing,
    genai_messages,
    init_sentry,
    record_chat_content,
    record_run_totals,
    record_usage,
    report_tool_crash,
    setup_tracing,
)


@pytest.fixture(autouse=True)
def sentry_off_afterwards():
    yield
    reset_sentry()  # replace any client a test started, so later tests are unaffected


async def crash_while_handling(body: dict) -> None:
    """Send `body` to an endpoint that reads it into a local variable and then fails."""
    app = FastAPI()

    @app.post("/boom")
    async def boom(payload: dict):
        customer_text = payload["question"]  # noqa: F841  (a local variable holding customer text, there when the error happens)
        raise RuntimeError("tool failed")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/boom", json=body, headers={"X-Merchant-Id": "acme"})
    sentry_sdk.flush()


def test_sentry_starts_only_with_a_dsn(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    started = []
    real_init = sentry_sdk.init
    monkeypatch.setattr(sentry_sdk, "init", lambda *args, **kwargs: started.append(kwargs) or real_init(*args, **kwargs))

    init_sentry()  # tests and local runs without .env have no DSN
    assert started == []

    init_sentry(FAKE_DSN, export_traces=False, transport=CatchingTransport())
    assert len(started) == 1
    assert sentry_sdk.get_client().is_active()


async def test_error_report_leaves_out_customer_words():
    secret = uuid.uuid4().hex  # made at run time so it appears in no source line (Sentry attaches nearby source code)
    transport = CatchingTransport()
    init_sentry(FAKE_DSN, export_traces=False, transport=transport)

    await crash_while_handling({"question": f"my card number is {secret}"})

    assert "RuntimeError" in transport.reports  # the report exists and says what went wrong
    assert "acme" in transport.reports  # the merchant header is kept, so errors can be filtered per merchant
    assert secret not in transport.reports  # neither the request body nor the local variable was sent


# --- traces ---
# Tracing is tested with a private provider and an in-memory exporter, so nothing is global and nothing is printed.


@pytest.fixture
def uninstrument_afterwards():
    """The instrumentations are process-wide switches: turn them off again so tests don't leak into each other."""
    yield
    PsycopgInstrumentor().uninstrument()
    HTTPXClientInstrumentor().uninstrument()


def test_trace_prints_as_a_tree_with_errors_marked():
    lines: list[str] = []
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(TraceTreeExporter(write=lines.append)))
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span("POST /ask"):
        with tracer.start_as_current_span("search_policy"):  # noqa: SIM117 (the nesting IS the span tree under test)
            with tracer.start_as_current_span("SELECT") as query:
                query.set_attribute("db.statement", "SELECT 1")
        with pytest.raises(KeyError), tracer.start_as_current_span("get_order"):
            raise KeyError("orders table is gone")

    [tree] = lines  # one trace, printed once, when the root span ended
    rows = tree.splitlines()
    names = [row.strip().split("  ")[0] for row in rows]
    indents = [len(row) - len(row.lstrip()) for row in rows]
    assert names == ["POST /ask", "search_policy", "SELECT", "get_order"]
    assert indents == [0, 2, 4, 2]
    assert "db.statement=SELECT 1" in tree
    assert "ms" in tree
    assert "ERROR KeyError:" in rows[3] and "orders table is gone" in rows[3]
    assert "ERROR" not in rows[0]  # the caller handled it


async def test_requests_are_traced_except_health_checks(
    uninstrument_afterwards,
):
    secret = uuid.uuid4().hex  # passed to the query as a parameter value; it must not end up in any span
    exporter = InMemorySpanExporter()
    app = FastAPI()
    setup_tracing(app, exporter)
    pool = await create_pool()  # opened AFTER tracing is on: connections opened earlier would not be traced

    @app.get("/health")
    async def health():
        return {}

    @app.get("/lookup")
    async def lookup():
        async with pool.connection() as conn:
            await conn.execute("SELECT item FROM orders WHERE merchant_id = %s AND id = %s", ("acme", secret))
        return {}

    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            await client.get("/health")  # monitors ping it constantly: a trace per ping would drown the real requests
            await client.get("/lookup")
    finally:
        FastAPIInstrumentor.uninstrument_app(app)
        await pool.close()

    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert "GET /health" not in spans
    request, query = spans["GET /lookup"], spans["SELECT"]
    assert query.parent.span_id == request.context.span_id  # the query is a child of the request
    assert request.attributes["http.route"] == "/lookup"
    assert not [s for s in spans.values() if "send" in s.name or "receive" in s.name]  # ASGI noise is excluded
    assert secret not in str([dict(s.attributes) for s in spans.values()])  # SQL text only, never parameter values


@pytest.mark.parametrize(
    "setting, expected_calls",
    [(None, 1), ("console", 1), ("off", 0)],
    ids=["unset_means_on", "console", "off_switch"],
)
def test_tracing_is_on_unless_switched_off(monkeypatch, setting, expected_calls):
    calls = []
    monkeypatch.setattr(observability, "setup_tracing", lambda *args, **kwargs: calls.append(args))
    if setting is None:
        monkeypatch.delenv("TRACING", raising=False)
    else:
        monkeypatch.setenv("TRACING", setting)

    enable_tracing(FastAPI())

    assert len(calls) == expected_calls


def test_tracing_reuses_an_existing_provider(monkeypatch):
    calls = []
    monkeypatch.setattr(observability, "setup_tracing", lambda app, exporter, **kwargs: calls.append(kwargs))
    monkeypatch.delenv("TRACING", raising=False)
    sdk_provider = TracerProvider()  # what Sentry's integration leaves behind

    monkeypatch.setattr(observability.trace, "get_tracer_provider", lambda: sdk_provider)
    enable_tracing(FastAPI())
    monkeypatch.setattr(observability.trace, "get_tracer_provider", lambda: observability.trace.NoOpTracerProvider())
    enable_tracing(FastAPI())

    assert calls == [{"provider": sdk_provider, "set_global": False}, {"provider": None, "set_global": True}]


# --- recording what the model said (GenAI conventions) ---

def attributes_of(record) -> dict:
    """Run `record(span)` on a throwaway span and return the attributes it set."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with provider.get_tracer("test").start_as_current_span("s") as span:
        record(span)
    return dict(exporter.get_finished_spans()[0].attributes)


ASK = ToolCall(id="c1", name="get_order", arguments={"order_id": "A100"})


def test_conversation_follows_genai_conventions():
    shaped = genai_messages([
        Message(role="user", content="Where is A100?"),
        Message(role="assistant", content="", tool_calls=[ASK]),
        Message(role="tool", content="headphones, delivered", tool_call_id="c1"),
    ])

    assert shaped == [
        {"role": "user", "parts": [{"type": "text", "content": "Where is A100?"}]},
        {"role": "assistant", "parts": [{"type": "tool_call", "id": "c1", "name": "get_order", "arguments": {"order_id": "A100"}}]},
        {"role": "tool", "parts": [{"type": "tool_call_response", "id": "c1", "result": "headphones, delivered"}]},
    ]


@pytest.mark.parametrize(
    "reply, finish_reason, first_part",
    [
        (LLMResponse(tool_calls=[ASK]), "tool_call", {"type": "tool_call", "id": "c1", "name": "get_order", "arguments": {"order_id": "A100"}}),
        (LLMResponse(text="It was delivered."), "stop", {"type": "text", "content": "It was delivered."}),
    ],
    ids=["calls_a_tool", "final_answer"],
)
def test_chat_span_records_input_and_output(reply, finish_reason, first_part):
    sent = [Message(role="user", content="Where is A100?")]

    attributes = attributes_of(lambda span: record_chat_content(span, sent, reply))

    assert json.loads(attributes["gen_ai.input.messages"])[0]["parts"][0]["content"] == "Where is A100?"
    [answer] = json.loads(attributes["gen_ai.output.messages"])
    assert answer["parts"][0] == first_part
    assert answer["finish_reason"] == finish_reason


def test_very_long_content_is_cut_off():
    sent = [Message(role="user", content="x" * (MAX_CONTENT_CHARS * 3))]

    attributes = attributes_of(lambda span: record_chat_content(span, sent, LLMResponse(text="ok")))

    assert len(attributes["gen_ai.input.messages"]) < MAX_CONTENT_CHARS + 50
    assert attributes["gen_ai.input.messages"].endswith("[truncated]")


# --- tokens and cost ---

USAGE = Usage(
    provider="anthropic", request_model="claude-sonnet-5-5", model="claude-sonnet-5-5-20261001",
    input_tokens=1000, output_tokens=50, cache_read_tokens=700, cache_write_tokens=200, cost_usd=0.02,
)


@pytest.mark.parametrize("trace_content", ["on", "off"], ids=["content_on", "content_off_tokens_are_not_content"])
def test_chat_span_records_model_tokens_and_cost(monkeypatch, trace_content):
    monkeypatch.setenv("TRACE_CONTENT", trace_content)

    attributes = attributes_of(lambda span: record_usage(span, USAGE))

    assert attributes == {
        "gen_ai.provider.name": "anthropic",
        "gen_ai.request.model": "claude-sonnet-5-5",  # what we asked for...
        "gen_ai.response.model": "claude-sonnet-5-5-20261001",  # ...and the dated model that answered
        "gen_ai.usage.input_tokens": 1000,  # the total, cached included: Sentry subtracts the cached counts to price the rest
        "gen_ai.usage.output_tokens": 50,
        "gen_ai.usage.cache_read.input_tokens": 700,
        "gen_ai.usage.cache_creation.input_tokens": 200,
        "app.cost_usd": 0.02,
    }


def test_run_cost_is_omitted_if_any_price_is_unknown():
    attributes = attributes_of(lambda span: record_run_totals(span, [USAGE, Usage(input_tokens=1, output_tokens=1)]))

    assert "app.cost_usd" not in attributes  # a partial sum would understate it
    assert attributes["app.input_tokens"] == 1001


# --- connecting traces to Sentry ---

@pytest.mark.parametrize("tracing, has_integration", [("console", True), ("off", False)], ids=["tracing_on", "tracing_off"])
def test_sentry_gets_traces_only_while_tracing_is_on(monkeypatch, tracing, has_integration):
    monkeypatch.setenv("TRACING", tracing)

    init_sentry(FAKE_DSN, export_traces=False, transport=CatchingTransport())

    assert (sentry_sdk.get_client().get_integration(OTLPIntegration) is not None) == has_integration


def test_one_failure_is_one_sentry_issue(sentry_events):
    client = sentry_sdk.get_client()
    assert client.get_integration(GoogleGenAIIntegration) is None
    assert client.get_integration(OpenAIIntegration) is None


# --- handled tool crashes become Sentry Issues (the loop-level report is in test_loop.py) ---

CRASH = ToolCall(id="c9", name="search_policy", arguments={"question": "return policy"})


def test_crashes_are_grouped_by_tool_and_error_type(sentry_events):
    report_tool_crash(CRASH, KeyError("a"))
    report_tool_crash(CRASH, KeyError("b"))  # same tool, same kind of error: the same Issue
    report_tool_crash(CRASH, ValueError("c"))  # another kind of error: another Issue
    report_tool_crash(ToolCall(id="c10", name="get_order", arguments={}), KeyError("d"))  # another tool: another Issue
    sentry_sdk.flush()

    fingerprints = [tuple(e["fingerprint"]) for e in sentry_events.events]
    assert fingerprints[0] == fingerprints[1]
    assert len(set(fingerprints)) == 3


def test_crash_report_can_hide_argument_values(sentry_events, monkeypatch):
    monkeypatch.setenv("TRACE_CONTENT", "off")
    secret = uuid.uuid4().hex
    call = ToolCall(id="c11", name="search_policy", arguments={"question": secret})

    report_tool_crash(call, KeyError("boom"))
    sentry_sdk.flush()

    [event] = sentry_events.events
    assert event["contexts"]["tool_call"]["arguments"] == ["question"]
    assert secret not in sentry_events.reports
