"""The harness core: the loop that turns a model into an agent."""

import time

from opentelemetry import trace
from pydantic import ValidationError

from refund_harness import metrics
from refund_harness.agent.llm import LLMClient
from refund_harness.agent.tools import ToolRegistry
from refund_harness.agent.types import (
    Message,
    RunResult,
    Step,
    StopReason,
    ToolCall,
    Usage,
)
from refund_harness.observability import (
    record_chat_content,
    record_run_totals,
    record_tool_call,
    record_tool_result,
    record_usage,
    report_tool_crash,
)

tracer = trace.get_tracer("agent_loop")

MAX_STEPS = 5  # tool rounds the application allows per question (the evals use the same limit)


async def run_agent(llm: LLMClient, tools: ToolRegistry, user_input: str, max_steps: int = MAX_STEPS) -> RunResult:
    """Model, tools, model, ... until the model answers without calling a tool, or after `max_steps` tool rounds.

    Tool failures go back to the model as results it can react to; model errors propagate to the caller.
    """
    started = time.perf_counter()
    stop_reason = "error"  # unless the loop below finishes
    try:
        result = await _run_agent(llm, tools, user_input, max_steps)
        stop_reason = result.stop_reason
        return result
    finally:
        metrics.AGENT_RUNS.labels(stop_reason).inc()
        metrics.AGENT_DURATION.observe(time.perf_counter() - started)


async def _run_agent(llm: LLMClient, tools: ToolRegistry, user_input: str, max_steps: int) -> RunResult:
    with tracer.start_as_current_span("invoke_agent") as agent_span:
        agent_span.set_attribute("gen_ai.operation.name", "invoke_agent")
        agent_span.set_attribute("gen_ai.agent.name", "refund-agent")
        messages: list[Message] = [Message(role="user", content=user_input)]
        tool_rounds = 0
        usages: list[Usage] = []
        timeline: list[Step] = []

        def finish(stop_reason: StopReason, final_text: str) -> RunResult:
            agent_span.set_attribute("app.steps", tool_rounds)
            agent_span.set_attribute("app.stop_reason", stop_reason)
            record_run_totals(agent_span, usages)
            return RunResult(final_text=final_text, stop_reason=stop_reason, messages=messages, steps=timeline)

        while True:
            with tracer.start_as_current_span("chat") as chat_span:
                chat_span.set_attribute("gen_ai.operation.name", "chat")
                call_started = time.perf_counter()
                try:
                    response = await llm.complete(messages, tools.specs())
                except Exception:
                    metrics.record_model_call(None, time.perf_counter() - call_started, failed=True)
                    raise
                call_seconds = time.perf_counter() - call_started
                metrics.record_model_call(response.usage, call_seconds)
                timeline.append(Step(
                    kind="model",
                    name=(response.usage and response.usage.model_name) or "model",
                    duration_ms=call_seconds * 1000,
                    output=response.text,
                    tool_calls=response.tool_calls,
                    usage=response.usage,
                ))
                record_chat_content(chat_span, messages, response)
                record_usage(chat_span, response.usage)
                if response.usage:
                    usages.append(response.usage)
                messages.append(
                    Message(
                        role="assistant",
                        content=response.text,
                        tool_calls=response.tool_calls,
                        provider_items=response.provider_items,
                    )
                )

            if not response.tool_calls:
                return finish("final", response.text)

            for call in response.tool_calls:
                step = await _run_tool_call(tools, call)
                timeline.append(step)
                messages.append(Message(role="tool", content=step.output, tool_call_id=call.id))
            tool_rounds += 1
            if tool_rounds >= max_steps:
                return finish("max_steps", response.text)


async def _run_tool_call(tools: ToolRegistry, call: ToolCall) -> Step:
    """Runs one tool call and returns what happened. Never raises: every failure becomes a result for the model."""
    if call.name not in tools.names():
        metrics.TOOL_CALLS.labels("(unknown)", "unknown_tool").inc()  # not call.name: the model could invent endless names
        output = f"Unknown tool {call.name}, available tools: {tools.names()}"
        return Step(kind="tool", name=call.name, duration_ms=0, arguments=call.arguments, output=output, failed=True)

    started = time.perf_counter()
    failed = True
    try:
        with tracer.start_as_current_span(f"execute_tool {call.name}") as tool_span:
            tool_span.set_attribute("gen_ai.operation.name", "execute_tool")
            tool_span.set_attribute("gen_ai.tool.name", call.name)
            record_tool_call(tool_span, call)
            output = await tools.get(call.name).run(call.arguments)
            record_tool_result(tool_span, output)
        failed = False
        metrics.TOOL_CALLS.labels(call.name, "ok").inc()
    except ValidationError as e:
        # The model's mistake, not a fault: it gets the details so it can correct the arguments and try again.
        output = f"Invalid arguments for {call.name}: {e}"
        metrics.TOOL_CALLS.labels(call.name, "invalid_arguments").inc()
    except Exception as e:  # noqa: BLE001 (any other failure is a fault in the tool: reported, and the run carries on)
        # The exception's own text can hold internals (SQL, hostnames) that the model might repeat to a customer.
        output = f"Tool {call.name} failed ({type(e).__name__})."
        metrics.TOOL_CALLS.labels(call.name, "crashed").inc()
        report_tool_crash(call, e)  # handled here, so Sentry would never hear of it otherwise
    duration_ms = (time.perf_counter() - started) * 1000
    return Step(kind="tool", name=call.name, duration_ms=duration_ms, arguments=call.arguments, output=output, failed=failed)
