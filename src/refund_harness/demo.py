"""The demo data: a believable day of refunds for both merchants, for the operator console.

Requests awaiting approval (two per merchant with linked-account notes), refunds the worker has paid, rejections with
their reasons, and last month's history for a few customers. Requests, approvals, rejections, payouts and notes all go
through the real code paths. Only the demo orders (with their customers, cards and addresses) are inserted directly, and
the timestamps are moved back afterwards so the day does not all happen at once.

reset_demo EMPTIES the refund tables and the stand-in provider's ledger first: in a demo they are scratch data. The demo
orders (C1xx for acme, G1xx for globex) sit next to the seed orders of db/init, which the evals use and this leaves alone.
Used by scripts/seed_demo.py and by POST /demo/reset (the console's "Reset demo data" button).
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from neo4j import AsyncDriver
from psycopg_pool import AsyncConnectionPool

from refund_harness.data.repository import approve_refund, reject_refund, submit_refund
from refund_harness.graph.risk import attach_risk_flags
from refund_harness.graph.sync import rebuild_merchant
from refund_harness.payments.client import PaymentProvider

log = logging.getLogger(__name__)

# (merchant, order, item, order total, delivered days ago or None if still on its way, customer, name, card, address)
# acme: max, nia and oli pay with the same card. globex: rosa, theo and uma ship to the same address. Their requests get
# linked-account notes.
ORDERS = [
    ("acme", "C101", "studio monitors", 24900, 12, "gia", "Gia Romano", "4417", "18 Harbor St 94107"),
    ("acme", "C102", "audio interface", 18900, 9, "hal", "Hal Okafor", "5520", "240 Valencia St 94103"),
    ("acme", "C103", "XLR cable", 1900, 7, "ivy", "Ivy Lindqvist", "6031", "9 Noe St 94114"),
    ("acme", "C104", "pop filter", 2400, 4, "jon", "Jon Bell", "7142", "1550 Hayes St 94117"),
    ("acme", "C105", "mic arm", 6900, 11, "kai", "Kai Nakamura", "8253", "77 Clement St 94118"),
    ("acme", "C106", "headphone amp", 15900, 14, "lea", "Lea Moreau", "9364", "301 Judah St 94122"),
    ("acme", "C107", "phono preamp", 12900, 5, "max", "Max Turner", "7777", "62 Dolores St 94103"),
    ("acme", "C108", "earbuds", 9900, 3, "nia", "Nia Patel", "7777", "415 Castro St 94114"),
    ("acme", "C109", "subwoofer", 34900, 16, "oli", "Oli Novak", "7777", "2 Linden St 94102"),
    ("acme", "C110", "speaker cable", 2900, 6, "pia", "Pia Santos", "1086", "830 Irving St 94122"),
    ("acme", "C111", "DAC", 21900, 10, "quinn", "Quinn Adler", "2197", "45 Fell St 94102"),
    ("acme", "C112", "turntable mat", 3400, 2, "rae", "Rae Kim", "3208", "1200 Taraval St 94116"),
    ("acme", "C113", "studio headphones", 14900, 8, "sky", "Sky Park", "4319", "560 Haight St 94117"),
    # earlier orders, so a customer's history has more than today's request in it
    ("acme", "C114", "bookshelf speakers", 19900, 52, "oli", "Oli Novak", "7777", "2 Linden St 94102"),
    ("acme", "C115", "USB microphone", 8900, 40, "max", "Max Turner", "7777", "62 Dolores St 94103"),
    ("acme", "C116", "headphone stand", 2900, 25, "max", "Max Turner", "7777", "62 Dolores St 94103"),
    ("acme", "C117", "guitar cable", 1500, 33, "nia", "Nia Patel", "7777", "415 Castro St 94114"),
    ("acme", "C118", "in-ear monitors", 17900, 21, "gia", "Gia Romano", "4417", "18 Harbor St 94107"),
    ("acme", "C119", "cable kit", 2400, 60, "jon", "Jon Bell", "7142", "1550 Hayes St 94117"),
    ("globex", "G101", "air fryer", 8900, 9, "hana", "Hana Sato", "5421", "88 Grove St 94102"),
    ("globex", "G102", "knife set", 14900, 4, "ike", "Ike Mensah", "6532", "19 Lyon St 94117"),
    ("globex", "G103", "kettle", 4900, 6, "jo", "Jo Walsh", "7643", "703 Page St 94117"),
    ("globex", "G104", "stand mixer", 32900, 12, "lena", "Lena Fischer", "2045", "9 Oak St 94117"),
    ("globex", "G105", "toaster", 5900, 5, "omar", "Omar Haddad", "3156", "1201 Fulton St 94117"),
    ("globex", "G106", "coffee grinder", 7900, 3, "rosa", "Rosa Lima", "4267", "77 Baker St 94115"),
    ("globex", "G107", "dutch oven", 18900, 8, "theo", "Theo Brandt", "5378", "77 Baker St 94115"),
    ("globex", "G108", "cast iron pan", 6400, 2, "uma", "Uma Reddy", "6489", "77 Baker St 94115"),
    ("globex", "G109", "espresso machine", 54900, 18, "vic", "Victor Hale", "7590", "330 Divisadero St 94117"),
    ("globex", "G110", "cutting board", 3900, 7, "hana", "Hana Sato", "5421", "88 Grove St 94102"),
    ("globex", "G111", "water filter", 2900, 10, "ike", "Ike Mensah", "6532", "19 Lyon St 94117"),
    ("globex", "G112", "food processor", 21900, 40, "theo", "Theo Brandt", "5378", "77 Baker St 94115"),
    ("globex", "G113", "rice cooker", 9900, 35, "rosa", "Rosa Lima", "4267", "77 Baker St 94115"),
    ("globex", "G114", "vacuum sealer", 12900, None, "lena", "Lena Fischer", "2045", "9 Oak St 94117"),
]

# (merchant, order, refund amount, minutes ago it was asked for, minutes until approved or None to leave it pending)
REQUESTS = [
    ("acme", "C114", 19900, 49 * 1440, 120),  # last month's refunds, for max and oli
    ("acme", "C115", 8900, 37 * 1440, 75),
    ("acme", "C109", 34900, 2980, 90),
    ("acme", "C101", 24900, 2640, 35),
    ("acme", "C102", 9450, 2400, 50),  # half: one of two units came back
    ("acme", "C103", 1900, 1210, 12),
    ("acme", "C105", 6900, 1090, 25),
    ("acme", "C106", 15900, 480, 40),
    ("acme", "C110", 2900, 300, 15),
    ("acme", "C112", 3400, 190, 20),
    ("acme", "C111", 21900, 240, None),
    ("acme", "C113", 14900, 150, None),
    ("acme", "C104", 2400, 70, None),
    ("acme", "C107", 12900, 42, None),
    ("acme", "C108", 4950, 6, None),  # one earbud
    ("acme", "C118", 17900, 2900, None),  # rejected, see REJECTED
    ("globex", "G101", 8900, 1320, 30),
    ("globex", "G103", 4900, 360, 45),
    ("globex", "G102", 14900, 110, None),
    ("globex", "G113", 9900, 33 * 1440, 90),  # last month's refunds, for rosa and theo
    ("globex", "G112", 21900, 37 * 1440, 60),
    ("globex", "G104", 32900, 2600, 45),
    ("globex", "G107", 18900, 700, 30),
    ("globex", "G111", 2900, 200, 20),
    ("globex", "G110", 3900, 1500, None),  # rejected, see REJECTED
    ("globex", "G109", 54900, 300, None),
    ("globex", "G108", 3200, 80, None),  # half: the lid was missing
    ("globex", "G106", 7900, 25, None),
]
# (merchant, order): (reason, minutes after the request that it was rejected)
REJECTED = {
    ("acme", "C118"): ("Carrier shows it delivered and signed for, and no return was started.", 140),
    ("globex", "G110"): ("Customer chose store credit instead.", 200),
}
REVIEWERS = {"acme": "maya@acme.example", "globex": "tom@globex.example"}
PAYOUT_WAIT_S = 30  # the worker pays the approved ones; it usually takes a few seconds


@dataclass(frozen=True)
class DemoReset:
    requests: int  # requests created
    unpaid: int  # approved requests the worker had not paid when we stopped waiting (0 unless it is not running)


async def reset_demo(
    pool: AsyncConnectionPool, driver: AsyncDriver | None, provider: PaymentProvider, payout_wait_s: float = PAYOUT_WAIT_S
) -> DemoReset:
    await provider.clear_ledger()  # old payouts would otherwise match no refund and show as exceptions
    await insert_orders(pool)
    async with pool.connection() as conn:
        await conn.execute("TRUNCATE TABLE refund_requests, refund_events, outbox")

    created = {}
    for merchant, order, cents, asked, _ in REQUESTS:
        async with pool.connection() as conn:
            # Eligibility is judged as of when the request was made: last month's refunds were in their window then.
            result = await submit_refund(conn, merchant, order, cents, datetime.now(UTC) - timedelta(minutes=asked))
        if result.refund_id is None:
            log.warning("demo request %s %s not created: %s %s", merchant, order, result.outcome, result.reasons)
            continue
        created[(merchant, order)] = result.refund_id

    for merchant, order, _, _, approve_after in REQUESTS:  # approving puts the payout job in the outbox: the worker pays
        if approve_after is not None and (merchant, order) in created:
            async with pool.connection() as conn:
                await approve_refund(conn, merchant, created[(merchant, order)], REVIEWERS[merchant])
    for (merchant, order), (reason, _) in REJECTED.items():
        async with pool.connection() as conn:
            await reject_refund(conn, merchant, created[(merchant, order)], REVIEWERS[merchant], reason)
    unpaid = await wait_for_payouts(pool, payout_wait_s)

    if driver is not None:
        for merchant in {m for m, _ in created}:  # after the payouts, so the graph sees the paid refunds too
            await rebuild_merchant(pool, driver, merchant)
    for merchant, order, _, asked, approve_after in REQUESTS:
        if (merchant, order) not in created:
            continue
        if approve_after is None and driver is not None:
            await attach_risk_flags(pool, driver, merchant, order, created[(merchant, order)])
        decided_after = REJECTED[(merchant, order)][1] if (merchant, order) in REJECTED else approve_after
        await backdate(pool, created[(merchant, order)], asked, decided_after)
    return DemoReset(requests=len(created), unpaid=unpaid)


async def insert_orders(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn, conn.transaction():
        for merchant, order, item, total, days, customer, name, last4, address in ORDERS:
            await conn.execute(
                "INSERT INTO customers (merchant_id, id, name) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (merchant, customer, name),
            )
            await conn.execute(
                "INSERT INTO payment_methods (merchant_id, id, fingerprint, last4) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (merchant, f"pm-{last4}", f"fp-demo-{last4}", last4),
            )
            cur = await conn.execute(
                """
                INSERT INTO addresses (merchant_id, normalized, display) VALUES (%s, %s, %s)
                ON CONFLICT (merchant_id, normalized) DO UPDATE SET display = EXCLUDED.display
                RETURNING id
                """,
                (merchant, address.lower(), address),
            )
            address_id = (await cur.fetchone())["id"]
            # Re-runs move delivery forward again, so the orders stay inside the refund window.
            await conn.execute(
                """
                INSERT INTO orders (merchant_id, id, item, total_cents, status, delivered_at,
                                    customer_id, payment_method_id, shipping_address_id)
                VALUES (%s, %s, %s, %s, %s, now() - make_interval(days => %s), %s, %s, %s)
                ON CONFLICT (merchant_id, id) DO UPDATE SET status = EXCLUDED.status, delivered_at = EXCLUDED.delivered_at
                """,
                (merchant, order, item, total, "shipped" if days is None else "delivered", days, customer, f"pm-{last4}", address_id),
            )


async def wait_for_payouts(pool: AsyncConnectionPool, wait_s: float) -> int:
    """How many approved requests are still unpaid once the worker has paid them all, or `wait_s` has passed."""
    deadline = time.monotonic() + wait_s
    while True:
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT count(*) AS n FROM refund_requests WHERE status = 'approved'")
            unpaid = (await cur.fetchone())["n"]
        if unpaid == 0 or time.monotonic() >= deadline:
            return unpaid
        await asyncio.sleep(0.5)


async def backdate(pool: AsyncConnectionPool, refund_id: int, asked_min: int, decided_after_min: int | None) -> None:
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            "UPDATE refund_requests SET created_at = now() - make_interval(mins => %s) WHERE id = %s", (asked_min, refund_id)
        )
        if decided_after_min is not None:  # approved (then issued 6 s later) or rejected
            decided = asked_min - decided_after_min
            await conn.execute(
                """
                UPDATE refund_events
                SET created_at = now() - make_interval(mins => %s)
                                 + CASE event WHEN 'issued' THEN interval '6 seconds' ELSE interval '0' END
                WHERE refund_id = %s
                """,
                (decided, refund_id),
            )
