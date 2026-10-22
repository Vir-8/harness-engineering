"""Resets the dev database to the demo data the operator console shows (src/refund_harness/demo.py).

    docker compose --profile app up -d           # the worker and provider must be running to pay the approved ones
    uv run python scripts/seed_demo.py           # EMPTIES the refund tables and the provider's ledger first

The console's "Reset demo data" button does the same.
"""

import asyncio

from refund_harness.data.db import create_pool
from refund_harness.demo import reset_demo
from refund_harness.graph.driver import create_graph_driver
from refund_harness.payments.client import PaymentProvider


async def main() -> None:
    provider = PaymentProvider.from_env()
    pool = await create_pool()
    driver = create_graph_driver()
    try:
        result = await reset_demo(pool, driver, provider)
        if result.unpaid:
            print(f"{result.unpaid} approved refunds were not paid in time: is the worker running?")
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT merchant_id, id, order_id, status, risk_flags FROM refund_requests ORDER BY id")
            for row in await cur.fetchall():
                flags = len(row["risk_flags"] or [])
                print(f"{row['merchant_id']:7} #{row['id']}  {row['order_id']}  {row['status']:9} {flags} note(s)")
    finally:
        await provider.aclose()
        await driver.close()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
