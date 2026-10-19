"""Observability: what the system reports about itself.

Errors go to Sentry (off unless SENTRY_DSN is set). Traces are produced by OpenTelemetry and printed as a tree
(on by default; TRACING=off turns it off).

Two separate privacy decisions live here:
- ERROR REPORTS to Sentry carry nothing but the error itself (type, message, stack, request path): no request
  bodies, no local variables, no cookies, no SQL parameter values, no model inputs or outputs.
- TRACES record what the model and tools said (see "what the model and tools actually said" below), switchable
  with TRACE_CONTENT=off. Customer questions, prompts and policy text can contain personal data.
"""

import json
import os
from collections import defaultdict
from collections.abc import Callable, Sequence

import sentry_sdk
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import StatusCode
from sentry_sdk.integrations.google_genai import GoogleGenAIIntegration
from sentry_sdk.integrations.otlp import OTLPIntegration

from refund_harness.agent.types import LLMResponse, Message, ToolCall, Usage

PRIVATE_BY_DEFAULT = {
    "user_info": False,
    "cookies": {"mode": "off"},
    "url_query_params": {"mode": "off"},
    "http_bodies": [],  # an empty list is the explicit opt-out
    "gen_ai": {"inputs": False, "outputs": False},
    "database_query_data": False,
    "stack_frame_variables": False,
}


def tracing_enabled() -> bool:
    """Tracing is on unless TRACING=off. One switch for the console tree, the auto-instrumentation and Sentry's traces."""
    return os.environ.get("TRACING", "console") != "off"


def init_sentry(
    dsn: str | None = None, *, export_traces: bool = True, collector_url: str | None = None, **overrides
) -> None:
    """Start error reporting, and trace export to Sentry. With no DSN (tests, a local run without .env) Sentry stays off.

    Traces reach Sentry through its OTLP integration, which adds an exporter to our OpenTelemetry provider. Sentry's
    own tracing must stay off (no traces_sample_rate), or everything would be traced twice.

    Call before the FastAPI app is created: Sentry hooks into the framework when it starts.
    `collector_url` sends traces to your own OpenTelemetry collector instead of Sentry (scripts/check_trace_export.py
    uses it). `export_traces=False` and `overrides` exist so tests can avoid the network and pass a fake transport.
    """
    dsn = dsn or os.environ.get("SENTRY_DSN")
    if not dsn:
        return
    otlp = OTLPIntegration(setup_otlp_traces_exporter=export_traces, collector_url=collector_url)
    integrations = [otlp] if tracing_enabled() else []
    sentry_sdk.init(
        dsn=dsn,
        environment=os.environ.get("APP_ENV", "development"),
        data_collection=PRIVATE_BY_DEFAULT,
        include_local_variables=False,
        integrations=integrations,
        # Sentry reports a Gemini failure by itself, from inside the SDK call, as a bare error with no idea which tool
        # it belongs to. The loop reports the same failure with that context (report_tool_crash), so one failure
        # would become two Issues. This integration would also add AI spans that duplicate ours.
        disabled_integrations=[GoogleGenAIIntegration()],
        **overrides,
    )


# --- traces ---

SERVICE_NAME = "refund-harness"
SHOWN_ATTRIBUTES = ("http.route", "http.status_code", "http.url", "db.statement")  # plus anything under SHOWN_PREFIXES
SHOWN_PREFIXES = ("gen_ai.", "app.")


def _shorten(value, limit: int = 100) -> str:
    """Content attributes can be whole conversations: the console tree shows only the start of each."""
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "..."


def _print_now(text: str) -> None:
    # flush: when stdout is a file or a pipe (containers, CI) Python would otherwise hold the output back
    print(text, flush=True)


class TraceTreeExporter(SpanExporter):
    """Prints each finished trace as an indented tree: one line per span, with its duration and key attributes.

    Spans arrive child-first (a span is reported when it ends), so spans are held per trace and the tree is
    printed when the root span (the one with no parent) ends.
    """

    def __init__(self, write: Callable[[str], None] = _print_now):
        self._write = write
        self._spans: dict[int, list[ReadableSpan]] = defaultdict(list)

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        for span in spans:
            self._spans[span.context.trace_id].append(span)
            if span.parent is None:
                self._write(self._render(self._spans.pop(span.context.trace_id)))
        return SpanExportResult.SUCCESS

    def _render(self, spans: list[ReadableSpan]) -> str:
        by_id = {s.context.span_id: s for s in spans}

        def depth(span: ReadableSpan) -> int:
            levels = 0
            while span.parent is not None and span.parent.span_id in by_id:
                span = by_id[span.parent.span_id]
                levels += 1
            return levels

        lines = []
        for span in sorted(spans, key=lambda s: s.start_time):
            millis = (span.end_time - span.start_time) / 1e6
            shown = {
                k: v for k, v in span.attributes.items() if k in SHOWN_ATTRIBUTES or k.startswith(SHOWN_PREFIXES)
            }
            details = "  " + " ".join(f"{k}={_shorten(v)}" for k, v in shown.items()) if shown else ""
            lines.append(f"{'  ' * depth(span)}{span.name}  {millis:.1f}ms{_error_note(span)}{details}")
        return "\n".join(lines)


def _error_note(span: ReadableSpan) -> str:
    """A span that failed says so, with the exception that did it. A plain terminal has no red, so we write ERROR."""
    if span.status.status_code != StatusCode.ERROR:
        return ""
    for event in span.events:
        if event.name == "exception":
            return f"  ERROR {event.attributes.get('exception.type')}: {_shorten(event.attributes.get('exception.message'))}"
    return f"  ERROR {span.status.description or ''}".rstrip()


def setup_tracing(
    app: FastAPI, exporter: SpanExporter, *, provider: TracerProvider | None = None, set_global: bool = False
) -> TracerProvider:
    """Turn on tracing for `app`, its database calls (psycopg) and its outgoing HTTP calls (httpx).

    `provider`: add to a provider that already exists (Sentry's integration creates one) instead of making a new one.
    set_global=True also makes a new provider the process-wide one, which our own spans use (trace.get_tracer).
    The instrumentations are told about the provider directly, so tests can use a private one.

    Call it BEFORE the database pool is created: psycopg is traced when a connection is opened, so connections
    that already exist stay untraced.
    """
    if provider is None:
        provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    if set_global:
        trace.set_tracer_provider(provider)
    PsycopgInstrumentor().instrument(tracer_provider=provider)
    HTTPXClientInstrumentor().instrument(tracer_provider=provider)
    # ASGI "send"/"receive" spans are empty noise for us, so they are excluded. So is /health: monitors ping it
    # constantly, and a trace per ping would drown out the real requests.
    FastAPIInstrumentor.instrument_app(
        app, tracer_provider=provider, exclude_spans=["send", "receive"], excluded_urls="/health"
    )
    return provider


def enable_tracing(app: FastAPI) -> None:
    """Tracing is on by default and prints each request's trace as a tree. TRACING=off turns it off (tests do)."""
    if not tracing_enabled():
        return
    existing = trace.get_tracer_provider()  # Sentry's integration may already have made one: add to it, don't compete
    reuse = existing if isinstance(existing, TracerProvider) else None
    setup_tracing(app, TraceTreeExporter(), provider=reuse, set_global=reuse is None)


# --- what the model and tools actually said ---
#
# Following OpenTelemetry's GenAI conventions: the conversation goes on the `chat` span as gen_ai.input.messages /
# gen_ai.output.messages, and a tool's arguments and result on the `execute_tool` span. These carry customer text,
# policy text and tool results, so they are one switch: TRACE_CONTENT=off records none of it (on by default here,
# because this project only has made-up data; turn it off before real customer data ever flows through).

MAX_CONTENT_CHARS = 8000  # one conversation can get long; a span attribute should not grow without limit


def content_tracing_on() -> bool:
    return os.environ.get("TRACE_CONTENT", "on") != "off"


def _as_attribute(value) -> str:
    text = json.dumps(value, default=str)
    return text if len(text) <= MAX_CONTENT_CHARS else text[:MAX_CONTENT_CHARS] + "...[truncated]"


def _parts(text: str, tool_calls: list[ToolCall]) -> list[dict]:
    parts = [{"type": "text", "content": text}] if text else []
    parts += [{"type": "tool_call", "id": c.id, "name": c.name, "arguments": c.arguments} for c in tool_calls]
    return parts


def genai_messages(messages: list[Message]) -> list[dict]:
    """Our Messages in the GenAI conventions' shape: a role, and a list of typed parts."""
    shaped = []
    for m in messages:
        if m.role == "tool":
            parts = [{"type": "tool_call_response", "id": m.tool_call_id, "result": m.content}]
        else:
            parts = _parts(m.content, m.tool_calls)
        shaped.append({"role": m.role, "parts": parts})
    return shaped


def record_chat_content(span: trace.Span, sent: list[Message], reply: LLMResponse) -> None:
    """On a `chat` span: the conversation the model was shown, and what it answered."""
    if not content_tracing_on():
        return
    finish_reason = "tool_call" if reply.tool_calls else "stop"
    reply_message = {"role": "assistant", "parts": _parts(reply.text, reply.tool_calls), "finish_reason": finish_reason}
    span.set_attribute("gen_ai.input.messages", _as_attribute(genai_messages(sent)))
    span.set_attribute("gen_ai.output.messages", _as_attribute([reply_message]))


def record_tool_call(span: trace.Span, call: ToolCall) -> None:
    """On an `execute_tool` span, while it is open and before the tool runs: what the model asked for."""
    if not content_tracing_on():
        return
    span.set_attribute("gen_ai.tool.call.id", call.id)
    span.set_attribute("gen_ai.tool.call.arguments", _as_attribute(call.arguments))


def record_tool_result(span: trace.Span, result: str) -> None:
    """On the same span, after the tool succeeded. A tool that crashed never gets a result recorded."""
    if content_tracing_on():
        span.set_attribute("gen_ai.tool.call.result", _as_attribute(result))


# --- tokens and cost ---

def record_usage(span: trace.Span, usage: Usage | None) -> None:
    """On a `chat` span: which model answered, how many tokens it used, and what the CLI says it cost.

    Not content, so the TRACE_CONTENT switch does not apply. The input total already includes the cached tokens,
    as the conventions require: Sentry subtracts the cached counts to price the rest.
    """
    if usage is None:
        return
    if usage.provider:
        span.set_attribute("gen_ai.provider.name", usage.provider)
    if usage.request_model or usage.model:
        span.set_attribute("gen_ai.request.model", usage.request_model or usage.model)
    if usage.model:
        span.set_attribute("gen_ai.response.model", usage.model)
    span.set_attribute("gen_ai.usage.input_tokens", usage.input_tokens)
    span.set_attribute("gen_ai.usage.output_tokens", usage.output_tokens)
    span.set_attribute("gen_ai.usage.cache_read.input_tokens", usage.cache_read_tokens)
    span.set_attribute("gen_ai.usage.cache_creation.input_tokens", usage.cache_write_tokens)
    if usage.cost_usd is not None:
        span.set_attribute("app.cost_usd", usage.cost_usd)  # no standard attribute for cost exists


def record_run_totals(span: trace.Span, usages: list[Usage]) -> None:
    """On the `invoke_agent` span: the whole run's totals. Named app.* so a backend that sums gen_ai.usage.* over
    the child spans does not count the same tokens twice."""
    if not usages:
        return
    span.set_attribute("app.input_tokens", sum(u.input_tokens for u in usages))
    span.set_attribute("app.output_tokens", sum(u.output_tokens for u in usages))
    costs = [u.cost_usd for u in usages if u.cost_usd is not None]
    if costs:
        span.set_attribute("app.cost_usd", round(sum(costs), 6))


# --- handled tool crashes ---

def report_tool_crash(call: ToolCall, error: Exception) -> None:
    """Make a tool crash that the loop HANDLED (and told the model about) visible in Sentry as an Issue.

    Sentry only reports what escapes, so without this a crashing tool would be invisible there. Crashes are grouped
    by tool and error type, tagged with the tool's name so they can be counted and filtered, and carry the arguments
    (values only while content tracing is on; otherwise just the argument names). A no-op when Sentry is off.
    """
    with sentry_sdk.new_scope() as scope:
        scope.set_tag("tool.name", call.name)
        scope.set_tag("error.type", type(error).__name__)
        scope.fingerprint = ["tool-crash", call.name, type(error).__name__]
        arguments = call.arguments if content_tracing_on() else sorted(call.arguments)
        scope.set_context("tool_call", {"id": call.id, "name": call.name, "arguments": arguments})
        sentry_sdk.capture_exception(error)
