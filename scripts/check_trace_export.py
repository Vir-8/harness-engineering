"""Checks that traces really leave the process, without needing Sentry or the network.

Starts a tiny local OTLP collector, points the app's trace export at it, runs one scripted agent run, and prints
the spans the collector received. If this prints the four spans, the same wiring will deliver them to Sentry.

    uv run python scripts/check_trace_export.py
"""

import asyncio
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ["TRACING"] = "console"
os.environ.pop("SENTRY_DSN", None)  # this script must never talk to the real Sentry

from opentelemetry import trace  # noqa: E402
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,  # noqa: E402
)
from pydantic import BaseModel  # noqa: E402

from refund_harness.agent.llm import FakeLLM  # noqa: E402
from refund_harness.agent.loop import run_agent  # noqa: E402
from refund_harness.agent.tools import Tool, ToolRegistry  # noqa: E402
from refund_harness.agent.types import LLMResponse, ToolCall, Usage  # noqa: E402
from refund_harness.observability import init_sentry  # noqa: E402

received: list[bytes] = []


class Collector(BaseHTTPRequestHandler):
    def do_POST(self):
        received.append(self.rfile.read(int(self.headers["Content-Length"])))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


class Args(BaseModel):
    order_id: str


async def get_order(args: Args) -> str:
    return "headphones, delivered"


def scripted_model() -> FakeLLM:
    usage = Usage(request_model="claude-sonnet-5-5", model="claude-sonnet-5-5", input_tokens=3000, output_tokens=100, cost_usd=0.02)
    return FakeLLM([
        LLMResponse(tool_calls=[ToolCall(id="c1", name="get_order", arguments={"order_id": "A100"})], usage=usage),
        LLMResponse(text="Your order was delivered.", usage=usage),
    ])


def main() -> int:
    server = HTTPServer(("127.0.0.1", 0), Collector)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    # the same call the app makes at startup, except the traces go to the local collector instead of Sentry
    init_sentry("https://publickey@o0.ingest.sentry.io/0", collector_url=f"http://127.0.0.1:{server.server_address[1]}/v1/traces")
    tools = ToolRegistry([Tool("get_order", "Look up an order.", Args, get_order)])
    asyncio.run(run_agent(scripted_model(), tools, "Where is order A100?"))
    trace.get_tracer_provider().force_flush()
    server.shutdown()

    names = []
    for body in received:
        request = ExportTraceServiceRequest()
        request.ParseFromString(body)
        for resource_spans in request.resource_spans:
            for scope_spans in resource_spans.scope_spans:
                for span in scope_spans.spans:
                    names.append(span.name)
                    print(f"  received: {span.name:26} {'(root)' if not span.parent_span_id else ''}")
    ok = sorted(names) == sorted(["invoke_agent", "chat", "execute_tool get_order", "chat"])
    print("OK: traces leave the process." if ok else f"PROBLEM: expected 4 spans, the collector got {names}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
