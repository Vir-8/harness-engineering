"""Advisory risk notes for a refund request, from the graph. They inform the human approver and decide NOTHING.

Three rules keep this safe:
  * It runs in code, after the refund is created. The model never calls it and never sees its result.
  * It can only add information. If Neo4j is down, slow or wrong, the refund is still created and the approver is told
    the check could not run ("graph_unavailable") instead of seeing silence that looks like "all clear".
  * The graph is a copy that lags Postgres (see graph/sync.py), so the flags describe the network as of the last sync.
"""

import asyncio
import logging

from neo4j import AsyncDriver
from psycopg_pool import AsyncConnectionPool

from refund_harness.data.repository import get_order_customer_id, set_risk_flags

log = logging.getLogger(__name__)

GRAPH_TIMEOUT_S = 2.0  # a side check must never hold up a refund for long
RISK_STEPS = 2  # how many customer-to-customer steps to look out (one step = a shared card or a shared address)
MANY_LINKED_CUSTOMERS = 2  # this many other customers in the network is worth a note
MANY_LINKED_REFUNDS = 2  # this many refund requests among those customers is worth a note

# A customer step is two relationships (customer to card or address, then on to the next customer).
LINKED_CUSTOMERS = (
    "MATCH (a:Customer {{merchant_id: $m, id: $id}})-[:USES|SHIPS_TO*1..{hops}]-(b:Customer) "
    "WHERE b <> a RETURN DISTINCT b.id AS id"
)
LINKED_REFUNDS = (
    "MATCH (c:Customer {merchant_id: $m})-[:PLACED]->(:Order {merchant_id: $m})-[:HAS_REFUND]->(r:Refund {merchant_id: $m}) "
    "WHERE c.id IN $ids RETURN count(r) AS n"
)

GRAPH_UNAVAILABLE = {
    "code": "graph_unavailable",
    "message": "The check for linked accounts could not run, so this request has NOT been checked for them.",
}
NO_CUSTOMER = {
    "code": "no_customer_recorded",
    "message": "This order has no customer on record, so it cannot be checked for linked accounts.",
}


def flags_from(linked_customers: int, linked_refunds: int, steps: int = RISK_STEPS) -> list[dict]:
    """Turn the two counts into notes. Pure, so the thresholds are easy to test and to change."""
    flags = []
    if linked_customers >= MANY_LINKED_CUSTOMERS:
        flags.append({
            "code": "linked_customers",
            "message": f"Linked to {linked_customers} other customers by a shared card or address, within {steps} steps.",
        })
    if linked_refunds >= MANY_LINKED_REFUNDS:
        flags.append({
            "code": "linked_refunds",
            "message": f"{linked_refunds} refund requests exist among the customers linked to this one.",
        })
    return flags


async def assess_customer(driver: AsyncDriver, merchant_id: str, customer_id: str | None) -> list[dict]:
    """The notes for one customer. Never raises: a failed or slow check comes back as a 'graph_unavailable' note."""
    if customer_id is None:
        return [NO_CUSTOMER]
    try:
        async with asyncio.timeout(GRAPH_TIMEOUT_S):
            records, _, _ = await driver.execute_query(
                LINKED_CUSTOMERS.format(hops=2 * RISK_STEPS), m=merchant_id, id=customer_id
            )
            linked = [record["id"] for record in records]
            refunds = 0
            if linked:
                records, _, _ = await driver.execute_query(LINKED_REFUNDS, m=merchant_id, ids=linked)
                refunds = records[0]["n"]
    except Exception:  # noqa: BLE001  (anything at all: this check is advisory and must not break the refund)
        log.warning("risk check could not run for customer %r of %r", customer_id, merchant_id, exc_info=True)
        return [GRAPH_UNAVAILABLE]
    return flags_from(len(linked), refunds)


async def attach_risk_flags(
    pool: AsyncConnectionPool, driver: AsyncDriver, merchant_id: str, order_id: str, refund_id: int
) -> None:
    """Run the check for the customer behind this refund and store the notes on it. Never raises.

    If even storing fails, the column stays NULL, which the approver sees as 'not checked'.
    """
    try:
        customer_id = await get_order_customer_id(pool, merchant_id, order_id)
        flags = await assess_customer(driver, merchant_id, customer_id)
        await set_risk_flags(pool, merchant_id, refund_id, flags)
    except Exception:  # noqa: BLE001
        log.exception("could not store risk flags for refund %s of %r", refund_id, merchant_id)
