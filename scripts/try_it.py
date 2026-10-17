"""Run one real question through the harness and print every step."""

import asyncio
import sys

from refund_harness.adapters.claude_cli import ClaudeCLIClient
from refund_harness.loop import run_agent
from refund_harness.refund_tools import build_registry


async def main(question: str) -> None:
    tools = build_registry()
    result = await run_agent(ClaudeCLIClient(), tools, question)

    for i, m in enumerate(result.messages):
        calls = [f"{c.name}({c.arguments})" for c in m.tool_calls]
        print(f"[{i}] {m.role:9} {m.content!r:.200} {calls or ''}")
    print(f"\nstop_reason={result.stop_reason}  assistant_turns={sum(m.role == 'assistant' for m in result.messages)}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "Can I get a refund for order A100?"))
