"""Graph vs SQL, measured: "who is linked to this customer through shared cards and addresses, within N steps?"

The same question is answered four ways, on a synthetic merchant ('bench') made of random customers who each pay with a
random card and ship to a random address from pools half the customer count, so about two customers share each card and
each address. A "step" is one hop from customer to customer through a shared card or address.

  sql-direct   a recursive query that joins the orders table to itself (what you would write with no preparation)
  sql-links    the same recursion over a precomputed (customer, card-or-address) table: closest to a graph's adjacency
  cypher-trails   Cypher's classic variable-length pattern, which enumerates every trail and then removes duplicates
  cypher-shortest Cypher's ANY SHORTEST form, which asks for the nearest route to each reachable customer only

A run is only reported if the engines AGREE on how many customers were reached: timing a wrong answer is meaningless.
Everything lives under merchant 'bench' and is removed afterwards, in Postgres and in the graph.
"""

import random
import statistics
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from neo4j import AsyncDriver, Query
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from refund_harness.graph.sync import LABELS, rebuild_merchant

BENCH = "bench"
ENGINES = ("sql-direct", "sql-links", "cypher-trails", "cypher-shortest")


class TimedOut(Exception):
    """A query ran past its time limit."""


# ---------------------------------------------------------------------------------------------------- the data

async def generate(pool: AsyncConnectionPool, customers: int, seed: float = 0.42, sharing: float = 2.0) -> None:
    """One order per customer, with a random card and a random address. Deterministic for a given seed.

    `sharing` is how many customers use each card (and each address) on average: the pools of cards and addresses have
    customers / sharing members. 1 means almost nobody shares; 5 means dense networks where clusters grow fast.
    """
    pool_size = max(int(customers / sharing), 1)
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("SELECT setseed(%s)", (seed,))
            await conn.execute("INSERT INTO merchants (id, name) VALUES (%s, %s)", (BENCH, BENCH))
            await conn.execute(
                "INSERT INTO customers (merchant_id, id, name) SELECT %s, 'c' || g, 'Customer ' || g FROM generate_series(0, %s) g",
                (BENCH, customers - 1),
            )
            await conn.execute(
                "INSERT INTO payment_methods (merchant_id, id, fingerprint, last4) "
                "SELECT %s, 'pm' || g, 'fp' || g, lpad((g %% 10000)::text, 4, '0') FROM generate_series(0, %s) g",
                (BENCH, pool_size - 1),
            )
            await conn.execute(
                "INSERT INTO addresses (merchant_id, normalized, display) SELECT %s, 'addr ' || g, 'Addr ' || g FROM generate_series(0, %s) g",
                (BENCH, pool_size - 1),
            )
            # the random numbers are drawn ONCE per customer in the inner query: a volatile function in a join
            # condition would be re-evaluated per candidate row and could not use the index
            await conn.execute(
                "INSERT INTO orders (merchant_id, id, item, total_cents, status, customer_id, payment_method_id, shipping_address_id) "
                "SELECT %(m)s, 'o' || r.g, 'thing', 1000, 'delivered', 'c' || r.g, 'pm' || r.card, a.id "
                "FROM (SELECT g, floor(random() * %(n)s)::int AS card, floor(random() * %(n)s)::int AS addr "
                "      FROM generate_series(0, %(last)s) g) r "
                "JOIN addresses a ON a.merchant_id = %(m)s AND a.normalized = 'addr ' || r.addr",
                {"m": BENCH, "n": pool_size, "last": customers - 1},
            )
            await conn.execute("ANALYZE orders")


async def remove_bench(pool: AsyncConnectionPool, driver: AsyncDriver) -> None:
    async with pool.connection() as conn:
        for table in ("refund_requests", "orders", "addresses", "payment_methods", "customers"):
            await conn.execute(f"DELETE FROM {table} WHERE merchant_id = %s", (BENCH,))  # noqa: S608 (fixed names)
        await conn.execute("DELETE FROM merchants WHERE id = %s", (BENCH,))
    for label in LABELS:
        await driver.execute_query(f"MATCH (n:{label} {{merchant_id: $m}}) DETACH DELETE n", m=BENCH)  # noqa: S608


# ---------------------------------------------------------------------------------------------------- the queries

SQL_DIRECT = """
WITH RECURSIVE reach(customer_id, depth) AS (
    SELECT %(start)s::text, 0
  UNION
    SELECT n.customer_id, r.depth + 1
    FROM reach r
    CROSS JOIN LATERAL (
        SELECT o2.customer_id FROM orders o1
        JOIN orders o2 ON o2.merchant_id = o1.merchant_id AND o2.payment_method_id = o1.payment_method_id
        WHERE o1.merchant_id = %(m)s AND o1.customer_id = r.customer_id
      UNION
        SELECT o2.customer_id FROM orders o1
        JOIN orders o2 ON o2.merchant_id = o1.merchant_id AND o2.shipping_address_id = o1.shipping_address_id
        WHERE o1.merchant_id = %(m)s AND o1.customer_id = r.customer_id
    ) n
    WHERE r.depth < %(depth)s
)
SELECT count(DISTINCT customer_id) - 1 AS reached FROM reach
"""

SQL_LINKS = """
WITH RECURSIVE reach(customer_id, depth) AS (
    SELECT %(start)s::text, 0
  UNION
    SELECT l2.customer_id, r.depth + 1
    FROM reach r
    JOIN bench_links l1 ON l1.customer_id = r.customer_id
    JOIN bench_links l2 ON l2.link_key = l1.link_key
    WHERE r.depth < %(depth)s
)
SELECT count(DISTINCT customer_id) - 1 AS reached FROM reach
"""

# A customer step is two relationships (customer to card or address, then on to the next customer), so N steps = 2N hops.
CYPHER_TRAILS = (
    "MATCH (a:Customer {{merchant_id: $m, id: $id}})-[:USES|SHIPS_TO*1..{hops}]-(b:Customer) "
    "WHERE b <> a RETURN count(DISTINCT b) AS reached"
)
CYPHER_SHORTEST = (
    "MATCH ANY SHORTEST (a:Customer {{merchant_id: $m, id: $id}})-[:USES|SHIPS_TO]-{{1,{hops}}}(b:Customer) "
    "WHERE b <> a RETURN count(DISTINCT b) AS reached"
)

LINKS_TABLE = """
CREATE TEMP TABLE bench_links AS
    SELECT DISTINCT customer_id, 'card:' || payment_method_id AS link_key FROM orders
        WHERE merchant_id = %(m)s AND customer_id IS NOT NULL AND payment_method_id IS NOT NULL
  UNION
    SELECT DISTINCT customer_id, 'addr:' || shipping_address_id::text FROM orders
        WHERE merchant_id = %(m)s AND customer_id IS NOT NULL AND shipping_address_id IS NOT NULL
"""


@dataclass
class Context:
    """What the queries need: one Postgres connection (the links table is private to it) and a Neo4j driver."""

    conn: AsyncConnection
    driver: AsyncDriver
    merchant: str = BENCH

    async def prepare_links(self) -> None:
        await self.conn.execute("DROP TABLE IF EXISTS bench_links")
        await self.conn.execute(LINKS_TABLE, {"m": self.merchant})
        await self.conn.execute("CREATE INDEX ON bench_links (customer_id)")
        await self.conn.execute("CREATE INDEX ON bench_links (link_key)")
        await self.conn.execute("ANALYZE bench_links")


async def _sql(ctx: Context, query: str, start: str, depth: int, timeout_s: float) -> int:
    import psycopg.errors

    try:
        async with ctx.conn.transaction():
            await ctx.conn.execute("SELECT set_config('statement_timeout', %s, true)", (str(int(timeout_s * 1000)),))
            cur = await ctx.conn.execute(query, {"start": start, "m": ctx.merchant, "depth": depth})
            return (await cur.fetchone())["reached"]
    except psycopg.errors.QueryCanceled as error:
        raise TimedOut from error


async def _cypher(ctx: Context, template: str, start: str, depth: int, timeout_s: float) -> int:
    from neo4j.exceptions import ClientError, TransientError

    query = Query(template.format(hops=2 * depth), timeout=timeout_s)
    try:
        records, _, _ = await ctx.driver.execute_query(query, m=ctx.merchant, id=start)
    except (TransientError, ClientError) as error:
        if "imed out" in str(error) or "imeout" in str(error) or "TransactionTimedOut" in str(error):
            raise TimedOut from error
        raise
    return records[0]["reached"]


async def reach(ctx: Context, engine: str, start: str, depth: int, timeout_s: float = 30) -> int:
    """How many OTHER customers can be reached from `start` within `depth` customer-to-customer steps."""
    if engine == "sql-direct":
        return await _sql(ctx, SQL_DIRECT, start, depth, timeout_s)
    if engine == "sql-links":
        return await _sql(ctx, SQL_LINKS, start, depth, timeout_s)
    if engine == "cypher-trails":
        return await _cypher(ctx, CYPHER_TRAILS, start, depth, timeout_s)
    if engine == "cypher-shortest":
        return await _cypher(ctx, CYPHER_SHORTEST, start, depth, timeout_s)
    raise ValueError(f"unknown engine {engine!r}")


# ---------------------------------------------------------------------------------------------------- the benchmark

@dataclass
class Cell:
    engine: str
    depth: int
    median_ms: float | None  # None when every run timed out (or the engine was dropped after timing out)
    timeouts: int
    runs: int
    median_reach: float | None


@dataclass
class BenchReport:
    customers: int
    sharing: float
    starts: int
    timeout_s: float
    load_s: float  # first sync into the graph
    rebuild_s: float  # delete the merchant's slice and reload it
    graph_nodes: int
    graph_relationships: int
    cells: list[Cell] = field(default_factory=list)
    disagreements: list[str] = field(default_factory=list)  # engines gave different answers: the run is NOT trustworthy


async def _timed(call: Callable[[], Awaitable[int]]) -> tuple[float | None, int | None]:
    started = time.perf_counter()
    try:
        value = await call()
    except TimedOut:
        return None, None
    return (time.perf_counter() - started) * 1000, value


async def run_benchmark(
    pool: AsyncConnectionPool,
    driver: AsyncDriver,
    *,
    customers: int,
    depths: list[int],
    starts: int,
    timeout_s: float,
    sharing: float = 2.0,
    engines: tuple[str, ...] = ENGINES,
    seed: int = 7,
    on_progress: Callable[[str], None] = lambda text: None,
) -> BenchReport:
    await remove_bench(pool, driver)  # in case an earlier run was interrupted
    try:
        on_progress(f"generating {customers:,} customers in Postgres")
        await generate(pool, customers, sharing=sharing)
        on_progress("loading the graph (first sync)")
        t0 = time.perf_counter()
        await sync_for_bench(pool, driver)
        load_s = time.perf_counter() - t0
        on_progress("rebuilding the graph (delete + reload)")
        t0 = time.perf_counter()
        await rebuild_merchant(pool, driver, BENCH)
        rebuild_s = time.perf_counter() - t0

        counts = (await driver.execute_query(
            "MATCH (n {merchant_id: $m}) WITH count(n) AS nodes OPTIONAL MATCH ({merchant_id: $m})-[r]->() RETURN nodes, count(r) AS rels", m=BENCH
        ))[0][0]
        report = BenchReport(customers, sharing, starts, timeout_s, load_s, rebuild_s, counts["nodes"], counts["rels"])

        chosen = random.Random(seed).sample(range(customers), min(starts, customers))
        start_ids = [f"c{i}" for i in chosen]
        async with pool.connection() as conn:
            ctx = Context(conn, driver)
            await ctx.prepare_links()
            alive = set(engines)
            for engine in engines:  # warm up each engine once, outside the timings
                await reach(ctx, engine, start_ids[0], min(depths), timeout_s)
            for depth in depths:
                answers: dict[str, dict[str, int]] = {}
                for engine in engines:
                    if engine not in alive:
                        report.cells.append(Cell(engine, depth, None, 0, 0, None))
                        continue
                    on_progress(f"depth {depth}: {engine}")
                    times, reached, timeouts = [], {}, 0
                    for start in start_ids:
                        ms, value = await _timed(lambda e=engine, s=start, d=depth: reach(ctx, e, s, d, timeout_s))
                        if ms is None:
                            timeouts += 1
                        else:
                            times.append(ms)
                            reached[start] = value
                    answers[engine] = reached
                    report.cells.append(Cell(
                        engine, depth, statistics.median(times) if times else None, timeouts, len(start_ids),
                        statistics.median(reached.values()) if reached else None,
                    ))
                    if timeouts * 2 >= len(start_ids):
                        alive.discard(engine)  # it will only get worse with depth: stop spending time on it
                _check_agreement(depth, answers, report)
        return report
    finally:
        await remove_bench(pool, driver)


async def sync_for_bench(pool: AsyncConnectionPool, driver: AsyncDriver) -> None:
    from refund_harness.graph.sync import sync_merchant

    await sync_merchant(pool, driver, BENCH)


def _check_agreement(depth: int, answers: dict[str, dict[str, int]], report: BenchReport) -> None:
    """Every engine that finished a start customer must report the same count as every other engine for that customer."""
    starts = set().union(*(a.keys() for a in answers.values())) if answers else set()
    for start in sorted(starts):
        seen = {engine: a[start] for engine, a in answers.items() if start in a}
        if len(set(seen.values())) > 1:
            report.disagreements.append(f"depth {depth}, start {start}: {seen}")


def render(report: BenchReport) -> str:
    depths = sorted({c.depth for c in report.cells})
    engines = [e for e in ENGINES if any(c.engine == e for c in report.cells)]
    by = {(c.engine, c.depth): c for c in report.cells}

    def cell(engine: str, depth: int) -> str:
        c = by[(engine, depth)]
        if c.runs == 0:
            return "gave up"
        if c.median_ms is None:
            return f"TIMEOUT ({c.timeouts}/{c.runs})"
        suffix = f" ({c.timeouts} timeouts)" if c.timeouts else ""
        return f"{c.median_ms:,.1f}{suffix}"

    reach_row = ["median customers reached"] + [
        f"{next((by[(e, d)].median_reach for e in engines if by[(e, d)].median_reach is not None), float('nan')):,.0f}" for d in depths
    ]
    header = ["median ms, by steps"] + [f"{d} step{'s' if d > 1 else ''}" for d in depths]
    rows = [header] + [[e] + [cell(e, d) for d in depths] for e in engines] + [reach_row]
    widths = [max(len(r[i]) for r in rows) for i in range(len(header))]
    table = "\n".join("  ".join(value.ljust(widths[i]) if i == 0 else value.rjust(widths[i]) for i, value in enumerate(r)) for r in rows)
    lines = [
        f"{report.customers:,} customers, about {report.sharing:g} per card and per address; "
        f"{report.graph_nodes:,} nodes and {report.graph_relationships:,} relationships in the graph",
        f"{report.starts} random start customers per cell; timeout {report.timeout_s:g}s",
        f"loading the graph from Postgres: {report.load_s:.1f}s   rebuilding it (delete + reload): {report.rebuild_s:.1f}s",
        "",
        table,
    ]
    if report.disagreements:
        lines += ["", "THE ENGINES DISAGREED (these timings cannot be trusted):", *report.disagreements[:10]]
    else:
        lines += ["", "all engines that finished gave the same answer for every start customer"]
    return "\n".join(lines)


async def run_and_render(pool: AsyncConnectionPool, driver: AsyncDriver, **kwargs) -> str:
    return render(await run_benchmark(pool, driver, **kwargs))


