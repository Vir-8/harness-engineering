"""Graph vs SQL: who is linked to a customer through shared cards and addresses, within N steps? Measured, not argued.

    uv run python scripts/bench_graph.py                          # 50,000 customers, 1 to 6 steps
    uv run python scripts/bench_graph.py --customers 5000 --depths 1,2,3 --starts 5
    uv run python scripts/bench_graph.py --sharing 5 --depths 1,2,3,4      # a denser network

Builds a synthetic merchant called 'bench' (random customers, cards and addresses), loads it into the graph, answers the
same question four ways, checks they AGREE, times them, and removes everything again. Needs both containers up.
"""

import argparse
import asyncio

from refund_harness.data.db import create_pool
from refund_harness.graph.bench import run_and_render
from refund_harness.graph.driver import create_graph_driver


async def main(args: argparse.Namespace) -> None:
    pool = await create_pool()
    try:
        async with create_graph_driver() as driver:
            print(await run_and_render(
                pool, driver, customers=args.customers, depths=[int(d) for d in args.depths.split(",")],
                starts=args.starts, timeout_s=args.timeout, sharing=args.sharing, on_progress=lambda text: print(f"  ... {text}", flush=True),
            ))
    finally:
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--customers", type=int, default=50_000)
    parser.add_argument("--depths", default="1,2,3,4,5,6", help="comma-separated numbers of steps")
    parser.add_argument("--sharing", type=float, default=2.0, help="average customers per card and per address: higher means denser networks")
    parser.add_argument("--starts", type=int, default=10, help="random start customers per cell")
    parser.add_argument("--timeout", type=float, default=10, help="seconds before a query counts as timed out")
    asyncio.run(main(parser.parse_args()))
