"""The harness core: the loop that turns a model into an agent."""

from opentelemetry import trace

from .llm import LLMClient
from .observability import (
    record_chat_content,
    record_run_totals,
    record_tool_call,
    record_tool_result,
    record_usage,
    report_tool_crash,
)
from .tools import ToolRegistry
from .types import Message, RunResult, Usage

tracer = trace.get_tracer("agent_loop")

async def run_agent(llm: LLMClient, tools: ToolRegistry, user_input: str, max_steps: int = 10) -> RunResult:
    with tracer.start_as_current_span("invoke_agent") as agent_span:
        agent_span.set_attribute("gen_ai.operation.name", "invoke_agent")
        agent_span.set_attribute("gen_ai.agent.name", "refund-agent")
        messages: list[Message] = [Message(role="user", content=user_input)]
        steps = 0
        usages: list[Usage] = []
        while True:
            with tracer.start_as_current_span("chat") as chat_span:
                chat_span.set_attribute("gen_ai.operation.name", "chat")
                response = await llm.complete(messages, tools.specs())
                record_chat_content(chat_span, messages, response)
                record_usage(chat_span, response.usage)
                if response.usage:
                    usages.append(response.usage)
                messages.append(
                    Message(role="assistant", content=response.text, tool_calls=response.tool_calls)
                )

            if not response.tool_calls:
                agent_span.set_attribute("app.steps", steps)
                agent_span.set_attribute("app.stop_reason", "final")
                record_run_totals(agent_span, usages)
                return RunResult(final_text=response.text, stop_reason="final", messages=messages)

            for call in response.tool_calls:
                if call.name not in tools.names():
                    chat_span.set_attribute("gen_ai.invalid_tool.name", call.name)
                    output = f"Unknown tool {call.name}, available tools: {tools.names()}"
                    messages.append(Message(role="tool", content=output, tool_call_id=call.id))
                    continue

                try:
                    with tracer.start_as_current_span(f"execute_tool {call.name}") as tool_span:
                        tool_span.set_attribute("gen_ai.operation.name", "execute_tool")
                        tool_span.set_attribute("gen_ai.tool.name", call.name)
                        record_tool_call(tool_span, call)
                        tool = tools.get(call.name)
                        output = await tool.run(call.arguments)
                        record_tool_result(tool_span, output)
                except Exception as e:
                    output = f"Tool crashed mid-run, error: {e}"
                    report_tool_crash(call, e)  # handled here, so Sentry would never hear of it otherwise
                messages.append(Message(role="tool", content=output, tool_call_id=call.id))
            steps += 1
            if steps >= max_steps:
                agent_span.set_attribute("app.steps", steps)
                agent_span.set_attribute("app.stop_reason", "max_steps")
                record_run_totals(agent_span, usages)
                return RunResult(final_text=response.text, stop_reason="max_steps", messages=messages)
