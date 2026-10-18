"""Run one real question through the whole harness (real model, real embeddings, real database) and print every step.

    uv run --env-file .env python scripts/try_it.py [merchant_id] "question"
"""

import asyncio
import sys

from refund_harness.adapters.claude_cli import ClaudeCLIClient
from refund_harness.adapters.gemini_embeddings import GeminiEmbedder
from refund_harness.db import create_pool
from refund_harness.loop import run_agent
from refund_harness.refund_tools import build_registry


async def main(merchant_id: str, question: str) -> None:
    pool = await create_pool()
    try:
        tools = build_registry(pool, merchant_id, GeminiEmbedder())
        result = await run_agent(ClaudeCLIClient(), tools, question)
    finally:
        await pool.close()

    for i, m in enumerate(result.messages):
        calls = [f"{c.name}({c.arguments})" for c in m.tool_calls]
        print(f"[{i}] {m.role:9} {m.content!r:.200} {calls or ''}")
    print(f"\nstop_reason={result.stop_reason}  assistant_turns={sum(m.role == 'assistant' for m in result.messages)}")


if __name__ == "__main__":
    args = sys.argv[1:]
    merchant = args[0] if len(args) == 2 else "acme"
    asyncio.run(main(merchant, args[-1] if args else "Can I get a refund for order A100?"))
