"""The graph-vs-SQL benchmark: do the four engines give the RIGHT answer, and does a run clean up after itself?

The README's benchmark numbers mean something only if every engine counts the same customers.
Exactness is checked on the hand-built ring (graph-a), where the answers are known:
ana reaches ben and cy in 1 step, eve in 2, fay in 3; dee reaches nobody.
"""

import pytest
from graph_helpers import A

from refund_harness.graph.bench import BENCH, ENGINES, Context, reach, run_benchmark
from refund_harness.graph.sync import sync_merchant

# (start customer, steps, how many OTHER customers can be reached)
KNOWN_ANSWERS = [
    ("ana", 1, 2), ("ana", 2, 3), ("ana", 3, 4), ("ana", 6, 4),   # ben, cy / + eve / + fay / nobody new
    ("ben", 1, 3),                                                # ana, cy (card) and eve (address)
    ("fay", 1, 1), ("fay", 3, 4),                                 # eve / then ben, ana, cy
    ("dee", 1, 0), ("dee", 6, 0),                                 # connected to nobody
]


@pytest.fixture
async def ring(world):
    """The ring in Postgres AND in the graph, and a context for the four engines to run against it."""
    await sync_merchant(world.pool, world.driver, A)
    async with world.pool.connection() as conn:
        ctx = Context(conn, world.driver, merchant=A)
        await ctx.prepare_links()
        yield ctx


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("start, steps, expected", KNOWN_ANSWERS)
async def test_every_engine_gives_the_known_answer(ring, engine, start, steps, expected):
    assert await reach(ring, engine, start, steps) == expected


async def test_benchmark_engines_agree_and_clean_up(pool, world):
    report = await run_benchmark(pool, world.driver, customers=300, depths=[1, 2], starts=3, timeout_s=30)

    assert report.disagreements == []
    assert {(c.engine, c.depth) for c in report.cells} == {(e, d) for e in ENGINES for d in (1, 2)}
    assert all(c.median_ms is not None and c.runs == 3 for c in report.cells)
    assert report.graph_nodes > 300 and report.load_s > 0 and report.rebuild_s > 0
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) AS n FROM orders WHERE merchant_id = %s", (BENCH,))
        assert (await cur.fetchone())["n"] == 0
    assert await world.count("MATCH (n {merchant_id: $m}) RETURN count(n) AS n", m=BENCH) == 0
