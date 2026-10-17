"""The harness core: the loop that turns a model into an agent."""

from .llm import LLMClient
from .tools import ToolRegistry
from .types import Message, RunResult


async def run_agent(llm: LLMClient, tools: ToolRegistry, user_input: str, max_steps: int = 10) -> RunResult:
    messages: list[Message] = [Message(role="user", content=user_input)]
    steps = 0
    while True:
        response = await llm.complete(messages, tools.specs())
        messages.append(
            Message(role="assistant", content=response.text, tool_calls=response.tool_calls)
        )

        if not response.tool_calls:
            return RunResult(final_text=response.text, stop_reason="final", messages=messages)

        for call in response.tool_calls:
            if call.name not in tools.names():
                output = f"Unknown tool {call.name}, available tools: {tools.names()}"
                messages.append(Message(role="tool", content=output, tool_call_id=call.id))
                continue

            try:
                tool = tools.get(call.name)
                output = await tool.run(call.arguments)
            except Exception as e:
                output = f"Tool crashed mid-run, error: {e}"
            messages.append(Message(role="tool", content=output, tool_call_id=call.id))
        steps += 1
        if steps >= max_steps:
            return RunResult(final_text=response.text, stop_reason="max_steps", messages=messages)
