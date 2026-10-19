"""Observability: error reports (Sentry), the trace tree printer and tracing setup, and recording content on spans.

For Sentry, a throwaway app raises an error while handling a request whose body holds a secret, and a fake transport
catches what would have been sent, so nothing leaves the machine. Tracing is tested with private providers and
in-memory exporters. The agent loop's own spans are in test_loop_spans.py.
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
from sentry_sdk.integrations.otlp import OTLPIntegration

from refund_harness import observability
from refund_harness.db import create_pool
from refund_harness.observability import (
    MAX_CONTENT_CHARS,
    TraceTreeExporter,
    enable_tracing,
    genai_messages,
    init_sentry,
    record_chat_content,
    record_run_totals,
    record_tool_call,
    record_tool_result,
    record_usage,
    report_tool_crash,
    setup_tracing,
)
from refund_harness.types import LLMResponse, Message, ToolCall, Usage


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


def test_without_a_dsn_sentry_is_never_started(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    started = []
    monkeypatch.setattr(sentry_sdk, "init", lambda *args, **kwargs: started.append(kwargs))

    init_sentry()

    assert started == []


def test_with_a_dsn_sentry_turns_on():
    init_sentry(FAKE_DSN, export_traces=False, transport=CatchingTransport())

    assert sentry_sdk.get_client().is_active()


async def test_an_error_report_has_the_error_and_the_merchant_but_not_the_customers_words():
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


def test_a_finished_trace_prints_as_an_indented_tree():
    lines: list[str] = []
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(TraceTreeExporter(write=lines.append)))
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span("POST /ask"):
        with tracer.start_as_current_span("search_policy"):
            with tracer.start_as_current_span("SELECT") as query:
                query.set_attribute("db.statement", "SELECT 1")
        with tracer.start_as_current_span("get_order"):
            pass

    [tree] = lines  # one trace, printed once, when the root span ended
    names = [line.strip().split("  ")[0] for line in tree.splitlines()]
    indents = [len(line) - len(line.lstrip()) for line in tree.splitlines()]
    assert names == ["POST /ask", "search_policy", "SELECT", "get_order"]
    assert indents == [0, 2, 4, 2]
    assert "db.statement=SELECT 1" in tree
    assert "ms" in tree


def test_nothing_is_printed_until_the_root_span_has_ended():
    lines: list[str] = []
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(TraceTreeExporter(write=lines.append)))
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span("root"):
        with tracer.start_as_current_span("child"):
            pass
        assert lines == []  # the child has ended, but the request is still running

    assert len(lines) == 1


def test_attributes_that_are_not_on_the_shown_list_stay_out_of_the_tree():
    lines: list[str] = []
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(TraceTreeExporter(write=lines.append)))

    with provider.get_tracer("test").start_as_current_span("root") as span:
        span.set_attribute("gen_ai.usage.input_tokens", 42)
        span.set_attribute("http.user_agent", "curl/8")

    assert "gen_ai.usage.input_tokens=42" in lines[0]
    assert "curl" not in lines[0]


async def test_a_request_and_its_database_query_become_nested_spans(uninstrument_afterwards):
    secret = uuid.uuid4().hex  # passed to the query as a parameter value; it must not end up in any span
    exporter = InMemorySpanExporter()
    app = FastAPI()
    setup_tracing(app, exporter)
    pool = await create_pool()  # opened AFTER tracing is on: connections opened earlier would not be traced

    @app.get("/lookup")
    async def lookup():
        async with pool.connection() as conn:
            await conn.execute("SELECT item FROM orders WHERE merchant_id = %s AND id = %s", ("acme", secret))
        return {}

    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            await client.get("/lookup")
    finally:
        FastAPIInstrumentor.uninstrument_app(app)
        await pool.close()

    spans = {s.name: s for s in exporter.get_finished_spans()}
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


# --- recording what the model and tools said (GenAI conventions) ---

def attributes_of(record) -> dict:
    """Run `record(span)` on a throwaway span and return the attributes it set."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with provider.get_tracer("test").start_as_current_span("s") as span:
        record(span)
    return dict(exporter.get_finished_spans()[0].attributes)


ASK = ToolCall(id="c1", name="get_order", arguments={"order_id": "A100"})


def test_a_conversation_is_shaped_as_roles_and_typed_parts():
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


def test_a_chat_span_records_what_the_model_was_shown_and_what_it_answered():
    sent = [Message(role="user", content="Where is A100?")]

    attributes = attributes_of(lambda span: record_chat_content(span, sent, LLMResponse(tool_calls=[ASK])))

    assert json.loads(attributes["gen_ai.input.messages"])[0]["parts"][0]["content"] == "Where is A100?"
    [answer] = json.loads(attributes["gen_ai.output.messages"])
    assert answer["parts"][0]["name"] == "get_order"
    assert answer["finish_reason"] == "tool_call"


def test_a_final_answer_finishes_with_stop():
    attributes = attributes_of(lambda span: record_chat_content(span, [], LLMResponse(text="It was delivered.")))

    [answer] = json.loads(attributes["gen_ai.output.messages"])
    assert answer["finish_reason"] == "stop"


def test_a_tool_span_records_the_arguments_and_the_result():
    def record(span):
        record_tool_call(span, ASK)
        record_tool_result(span, "headphones, delivered")

    attributes = attributes_of(record)

    assert attributes["gen_ai.tool.call.id"] == "c1"
    assert json.loads(attributes["gen_ai.tool.call.arguments"]) == {"order_id": "A100"}
    assert json.loads(attributes["gen_ai.tool.call.result"]) == "headphones, delivered"


def test_a_tool_that_failed_has_arguments_but_no_result():
    attributes = attributes_of(lambda span: record_tool_call(span, ASK))  # the result call is never reached

    assert "gen_ai.tool.call.arguments" in attributes
    assert "gen_ai.tool.call.result" not in attributes


def test_trace_content_off_records_nothing(monkeypatch):
    monkeypatch.setenv("TRACE_CONTENT", "off")

    def record(span):
        record_chat_content(span, [Message(role="user", content="secret")], LLMResponse(text="secret"))
        record_tool_call(span, ASK)
        record_tool_result(span, "secret")

    assert attributes_of(record) == {}


def test_very_long_content_is_cut_off():
    sent = [Message(role="user", content="x" * (MAX_CONTENT_CHARS * 3))]

    attributes = attributes_of(lambda span: record_chat_content(span, sent, LLMResponse(text="ok")))

    assert len(attributes["gen_ai.input.messages"]) < MAX_CONTENT_CHARS + 50
    assert attributes["gen_ai.input.messages"].endswith("[truncated]")


def test_the_console_tree_shows_only_the_start_of_a_long_attribute():
    lines: list[str] = []
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(TraceTreeExporter(write=lines.append)))

    with provider.get_tracer("test").start_as_current_span("chat") as span:
        span.set_attribute("gen_ai.input.messages", "y" * 5000)

    assert len(lines[0]) < 300
    assert lines[0].endswith("...")


def test_a_span_that_failed_is_marked_error_with_its_exception():
    lines: list[str] = []
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(TraceTreeExporter(write=lines.append)))
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span("agent"):
        with pytest.raises(KeyError):
            with tracer.start_as_current_span("execute_tool search_policy"):
                raise KeyError("policy table is gone")
        with tracer.start_as_current_span("execute_tool get_order"):
            pass

    rows = {row.strip().split("  ")[0]: row for row in lines[0].splitlines()}
    assert "ERROR KeyError:" in rows["execute_tool search_policy"]
    assert "policy table is gone" in rows["execute_tool search_policy"]
    assert "ERROR" not in rows["execute_tool get_order"]
    assert "ERROR" not in rows["agent"]  # the caller handled it


# --- tokens and cost ---

USAGE = Usage(
    provider="anthropic", model="claude-opus-4-6", input_tokens=1000, output_tokens=50,
    cache_read_tokens=700, cache_write_tokens=200, cost_usd=0.02,
)


def test_a_chat_span_records_the_model_tokens_and_cost():
    attributes = attributes_of(lambda span: record_usage(span, USAGE))

    assert attributes["gen_ai.provider.name"] == "anthropic"
    assert attributes["gen_ai.request.model"] == "claude-opus-4-6"
    assert attributes["gen_ai.response.model"] == "claude-opus-4-6"
    assert attributes["gen_ai.usage.input_tokens"] == 1000
    assert attributes["gen_ai.usage.output_tokens"] == 50
    assert attributes["gen_ai.usage.cache_read.input_tokens"] == 700
    assert attributes["gen_ai.usage.cache_creation.input_tokens"] == 200
    assert attributes["app.cost_usd"] == 0.02


def test_cached_tokens_are_never_more_than_the_input_total():
    """Sentry subtracts the cached counts from the input total to price the rest: a bigger cached count goes negative."""
    attributes = attributes_of(lambda span: record_usage(span, USAGE))

    cached = attributes["gen_ai.usage.cache_read.input_tokens"] + attributes["gen_ai.usage.cache_creation.input_tokens"]
    assert cached <= attributes["gen_ai.usage.input_tokens"]


def test_unknown_usage_records_nothing():
    assert attributes_of(lambda span: record_usage(span, None)) == {}


def test_token_counts_are_recorded_even_with_content_tracing_off(monkeypatch):
    monkeypatch.setenv("TRACE_CONTENT", "off")  # tokens are not content

    attributes = attributes_of(lambda span: record_usage(span, USAGE))

    assert attributes["gen_ai.usage.input_tokens"] == 1000


def test_run_totals_add_up_the_model_calls_under_names_a_backend_will_not_double_count():
    second = Usage(input_tokens=500, output_tokens=25, cost_usd=0.01)

    attributes = attributes_of(lambda span: record_run_totals(span, [USAGE, second]))

    assert attributes == {"app.input_tokens": 1500, "app.output_tokens": 75, "app.cost_usd": 0.03}


def test_run_totals_skip_calls_whose_cost_is_unknown():
    attributes = attributes_of(lambda span: record_run_totals(span, [USAGE, Usage(input_tokens=1, output_tokens=1)]))

    assert attributes["app.cost_usd"] == 0.02
    assert attributes["app.input_tokens"] == 1001


def test_no_model_calls_means_no_totals():
    assert attributes_of(lambda span: record_run_totals(span, [])) == {}


def test_the_requested_and_the_answering_model_are_recorded_separately():
    usage = Usage(request_model="claude-sonnet-5-5", model="claude-sonnet-5-5-20261001", input_tokens=1, output_tokens=1)

    attributes = attributes_of(lambda span: record_usage(span, usage))

    assert attributes["gen_ai.request.model"] == "claude-sonnet-5-5"
    assert attributes["gen_ai.response.model"] == "claude-sonnet-5-5-20261001"


# --- connecting traces to Sentry ---

def test_sentry_receives_traces_through_the_otlp_integration(monkeypatch):
    monkeypatch.setenv("TRACING", "console")

    init_sentry(FAKE_DSN, export_traces=False, transport=CatchingTransport())

    assert sentry_sdk.get_client().get_integration(OTLPIntegration) is not None


def test_with_tracing_off_sentry_gets_no_trace_integration(monkeypatch):
    monkeypatch.setenv("TRACING", "off")

    init_sentry(FAKE_DSN, export_traces=False, transport=CatchingTransport())

    assert sentry_sdk.get_client().get_integration(OTLPIntegration) is None


def test_sentrys_own_google_genai_reporting_is_off_so_one_failure_is_one_issue(sentry_events):
    assert sentry_sdk.get_client().get_integration(GoogleGenAIIntegration) is None


def test_setup_tracing_adds_to_a_provider_that_already_exists_instead_of_making_another(uninstrument_afterwards):
    existing = TracerProvider()  # what Sentry's integration leaves behind

    returned = setup_tracing(FastAPI(), InMemorySpanExporter(), provider=existing)

    assert returned is existing


def test_enable_tracing_joins_an_existing_sdk_provider_and_makes_one_when_there_is_none(monkeypatch):
    calls = []
    monkeypatch.setattr(observability, "setup_tracing", lambda app, exporter, **kwargs: calls.append(kwargs))
    monkeypatch.delenv("TRACING", raising=False)
    sdk_provider = TracerProvider()

    monkeypatch.setattr(observability.trace, "get_tracer_provider", lambda: sdk_provider)
    enable_tracing(FastAPI())
    monkeypatch.setattr(observability.trace, "get_tracer_provider", lambda: observability.trace.NoOpTracerProvider())
    enable_tracing(FastAPI())

    assert calls == [{"provider": sdk_provider, "set_global": False}, {"provider": None, "set_global": True}]


# --- handled tool crashes become Sentry Issues ---

CRASH = ToolCall(id="c9", name="search_policy", arguments={"question": "return policy"})


def test_a_tool_crash_becomes_an_issue_tagged_with_the_tool_and_carrying_its_arguments(sentry_events):
    report_tool_crash(CRASH, KeyError("policy table is gone"))
    sentry_sdk.flush()

    [event] = sentry_events.events
    assert event["tags"]["tool.name"] == "search_policy"
    assert event["tags"]["error.type"] == "KeyError"
    assert event["contexts"]["tool_call"] == {"id": "c9", "name": "search_policy", "arguments": {"question": "return policy"}}


def test_crashes_are_grouped_by_tool_and_error_type(sentry_events):
    report_tool_crash(CRASH, KeyError("a"))
    report_tool_crash(CRASH, KeyError("b"))  # same tool, same kind of error: the same Issue
    report_tool_crash(CRASH, ValueError("c"))  # another kind of error: another Issue
    report_tool_crash(ToolCall(id="c10", name="get_order", arguments={}), KeyError("d"))  # another tool: another Issue
    sentry_sdk.flush()

    fingerprints = [tuple(e["fingerprint"]) for e in sentry_events.events]
    assert fingerprints[0] == fingerprints[1]
    assert len(set(fingerprints)) == 3


def test_with_content_tracing_off_a_crash_report_has_argument_names_but_not_values(sentry_events, monkeypatch):
    monkeypatch.setenv("TRACE_CONTENT", "off")
    secret = uuid.uuid4().hex
    call = ToolCall(id="c11", name="search_policy", arguments={"question": secret})

    report_tool_crash(call, KeyError("boom"))
    sentry_sdk.flush()

    [event] = sentry_events.events
    assert event["contexts"]["tool_call"]["arguments"] == ["question"]
    assert secret not in sentry_events.reports


def test_reporting_a_crash_is_harmless_when_sentry_is_off():
    report_tool_crash(CRASH, KeyError("nobody is listening"))  # must not raise


async def test_health_checks_are_not_traced_but_real_requests_are(uninstrument_afterwards):
    exporter = InMemorySpanExporter()
    app = FastAPI()
    setup_tracing(app, exporter)

    @app.get("/health")
    async def health():
        return {}

    @app.get("/real")
    async def real():
        return {}

    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            await client.get("/health")
            await client.get("/real")
    finally:
        FastAPIInstrumentor.uninstrument_app(app)

    assert [s.name for s in exporter.get_finished_spans()] == ["GET /real"]
