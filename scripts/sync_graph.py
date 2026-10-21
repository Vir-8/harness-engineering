"""Copies merchants from Postgres into the Neo4j graph (safe to run again: it only adds and updates).

    uv run python scripts/sync_graph.py                # every merchant
    uv run python scripts/sync_graph.py acme globex    # just these

Needs the two containers up (docker compose up -d). No API keys.
"""

import asyncio
import sys

from refund_harness.data.db import create_pool
from refund_harness.graph.driver import create_graph_driver
from refund_harness.graph.sync import sync_merchant


async def main(merchants: list[str]) -> None:
    pool = await create_pool()
    try:
        if not merchants:
            async with pool.connection() as conn:
                cur = await conn.execute("SELECT id FROM merchants ORDER BY id")
                merchants = [row["id"] for row in await cur.fetchall()]
        async with create_graph_driver() as driver:
            for merchant in merchants:
                loaded = await sync_merchant(pool, driver, merchant)
                print(f"{merchant}: " + ", ".join(f"{count} {name}" for name, count in loaded.items()))
            records, _, _ = await driver.execute_query("MATCH (n) RETURN labels(n)[0] AS label, count(*) AS n ORDER BY label")
            print("\nin the graph now: " + ", ".join(f"{r['n']} {r['label']}" for r in records))
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
