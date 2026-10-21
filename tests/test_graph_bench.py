"""The graph-vs-SQL benchmark: do the four engines give the RIGHT answer, and does a run clean up after itself?

Exactness is checked on the hand-built ring (graph-a), where the answers are known:
ana reaches ben and cy in 1 step, eve in 2, fay in 3; dee reaches nobody.
"""

import pytest
from graph_helpers import A

from refund_harness.graph.bench import (
    BENCH,
    ENGINES,
    Context,
    TimedOut,
    _cypher,
    _sql,
    reach,
    render,
    run_benchmark,
)
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
async def test_every_engine_gives_the_known_answer_on_the_ring(ring, engine, start, steps, expected):
    assert await reach(ring, engine, start, steps) == expected


async def test_an_unknown_engine_is_refused(ring):
    with pytest.raises(ValueError):
        await reach(ring, "carrier-pigeon", "ana", 1)


async def test_a_slow_sql_query_is_reported_as_timed_out_not_as_a_wrong_answer(ring):
    slow = "SELECT count(*) AS reached FROM pg_sleep(2)"  # the unused parameters _sql passes along are simply ignored

    with pytest.raises(TimedOut):
        await _sql(ring, slow, "ana", 1, timeout_s=0.2)


async def test_a_slow_cypher_query_is_reported_as_timed_out_not_as_a_wrong_answer(ring):
    slow = "UNWIND range(1, 500000000) AS x WITH count(x) AS reached RETURN reached + 0 * {hops}"

    with pytest.raises(TimedOut):
        await _cypher(ring, slow, "ana", 1, timeout_s=0.2)


async def test_an_ordinary_error_is_not_mistaken_for_a_timeout(ring):
    with pytest.raises(Exception) as caught:
        await _cypher(ring, "THIS IS NOT CYPHER {hops}", "ana", 1, timeout_s=5)

    assert not isinstance(caught.value, TimedOut)


async def test_a_small_benchmark_runs_end_to_end_with_all_engines_agreeing_and_cleans_up_after_itself(pool, world):
    report = await run_benchmark(pool, world.driver, customers=300, depths=[1, 2], starts=3, timeout_s=30)

    assert report.disagreements == []
    assert {(c.engine, c.depth) for c in report.cells} == {(e, d) for e in ENGINES for d in (1, 2)}
    assert all(c.median_ms is not None and c.runs == 3 for c in report.cells)
    assert report.graph_nodes > 300 and report.load_s > 0 and report.rebuild_s > 0
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) AS n FROM orders WHERE merchant_id = %s", (BENCH,))
        assert (await cur.fetchone())["n"] == 0
    assert await world.count("MATCH (n {merchant_id: $m}) RETURN count(n) AS n", m=BENCH) == 0


async def test_the_benchmark_cleans_up_even_when_it_fails_halfway(pool, world):
    with pytest.raises(ValueError):
        await run_benchmark(pool, world.driver, customers=200, depths=[1], starts=2, timeout_s=30, engines=("sql-direct", "nonsense"))

    async with pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) AS n FROM customers WHERE merchant_id = %s", (BENCH,))
        assert (await cur.fetchone())["n"] == 0
    assert await world.count("MATCH (n {merchant_id: $m}) RETURN count(n) AS n", m=BENCH) == 0


async def test_the_report_says_when_the_engines_disagreed_so_timings_are_not_trusted(pool, world, monkeypatch):
    from refund_harness.graph import bench

    real = bench.reach

    async def wrong_for_cypher(ctx, engine, start, depth, timeout_s=30):
        value = await real(ctx, engine, start, depth, timeout_s)
        return value + 1 if engine == "cypher-trails" else value

    monkeypatch.setattr(bench, "reach", wrong_for_cypher)

    report = await run_benchmark(pool, world.driver, customers=200, depths=[1], starts=2, timeout_s=30)

    assert report.disagreements
    assert "DISAGREED" in render(report)


async def test_the_rendered_report_has_a_row_per_engine_and_a_column_per_depth(pool, world):
    text = render(await run_benchmark(pool, world.driver, customers=200, depths=[1, 2], starts=2, timeout_s=30))

    for engine in ENGINES:
        assert engine in text
    assert "1 step" in text and "2 steps" in text and "median customers reached" in text


async def test_the_sharing_setting_controls_how_many_cards_and_addresses_there_are(pool, world):
    from refund_harness.graph.bench import generate, remove_bench

    await remove_bench(pool, world.driver)
    try:
        await generate(pool, 400, sharing=4)
        async with pool.connection() as conn:
            cards = (await (await conn.execute("SELECT count(*) AS n FROM payment_methods WHERE merchant_id = %s", (BENCH,))).fetchone())["n"]
            addresses = (await (await conn.execute("SELECT count(*) AS n FROM addresses WHERE merchant_id = %s", (BENCH,))).fetchone())["n"]
            orders = (await (await conn.execute("SELECT count(*) AS n FROM orders WHERE merchant_id = %s", (BENCH,))).fetchone())["n"]
    finally:
        await remove_bench(pool, world.driver)

    assert (cards, addresses, orders) == (100, 100, 400)  # 400 customers / 4 per card


async def test_the_report_states_how_dense_the_network_was(pool, world):
    text = render(await run_benchmark(pool, world.driver, customers=200, depths=[1], starts=2, timeout_s=30, sharing=3))

    assert "about 3 per card and per address" in text
