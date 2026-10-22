"""The operator endpoints: the refund list and detail, a customer, the activity feed, the ledger, the ops summary and the
agent timeline. Each needs X-Reviewer-Id and shows only the X-Merchant-Id merchant's data.
"""

import pytest
from helpers import (
    A100_TOTAL_CENTS,
    ACME,
    GLOBEX,
    REVIEWER,
    approve,
    approved_refund,
    request,
)

from refund_harness.app import app, get_provider, get_sqs
from refund_harness.payments.payout import issue_refund


@pytest.fixture
def with_provider(provider):
    app.dependency_overrides[get_provider] = lambda: provider
    return provider


# --- refunds ---

async def test_list_shows_order_and_customer(client, pool, refunds):
    refund_id = await approved_refund(pool)

    [row] = (await client.get("/refunds", headers=REVIEWER)).json()

    assert row["refund_id"] == refund_id
    assert (row["status"], row["item"], row["amount_cents"]) == ("approved", "headphones", A100_TOTAL_CENTS)
    assert (row["customer_id"], row["customer_name"]) == ("ana", "Ana Alvarez")


async def test_detail_has_the_audit_trail(client, pool, refunds, provider):
    refund_id = await approved_refund(pool)
    await issue_refund(pool, provider, ACME, refund_id)

    detail = (await client.get(f"/refunds/{refund_id}", headers=REVIEWER)).json()

    assert [(e["event"], e["actor"]) for e in detail["events"]] == [("approved", "reviewer-1"), ("issued", "payout")]
    assert detail["status"] == "issued" and detail["provider_refund_id"].startswith("re_")


async def test_another_merchant_sees_none_of_it(client, pool, refunds):
    refund_id = await approved_refund(pool)
    globex = {**REVIEWER, "X-Merchant-Id": GLOBEX}

    assert (await client.get("/refunds", headers=globex)).json() == []
    assert (await client.get(f"/refunds/{refund_id}", headers=globex)).status_code == 404
    assert (await client.get("/activity", headers=globex)).json() == []
    assert (await client.get("/customers/ana", headers=globex)).status_code == 404  # acme's customer


# --- customers and activity ---

async def test_customer_comes_with_their_orders(client, pool, refunds):
    refund_id = await request(pool, ACME, "A100", 2000)

    response = await client.get("/customers/ana", headers=REVIEWER)

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Ana Alvarez"
    [order] = body["orders"]
    assert (order["order_id"], order["card_last4"], order["address"]) == ("A100", "1111", "9 Pine Ave, 94110")
    assert (order["refund_id"], order["refund_cents"], order["refund_status"]) == (refund_id, 2000, "pending")  # the latest request


async def test_activity_is_newest_first(client, pool, refunds):
    refund_id = await request(pool, ACME, "A100", A100_TOTAL_CENTS)
    await approve(pool, refund_id, actor="maya")

    items = (await client.get("/activity", headers=REVIEWER)).json()

    assert [(i["event"], i["actor"], i["refund_id"]) for i in items] == [("approved", "maya", refund_id), ("filed", "agent", refund_id)]
    assert (items[0]["customer_name"], items[0]["item"], items[0]["amount_cents"]) == ("Ana Alvarez", "headphones", A100_TOTAL_CENTS)


# --- money and the pipeline ---

async def test_ledger_matches_ours_with_the_providers(client, pool, refunds, with_provider):
    paid_id = await approved_refund(pool)
    await issue_refund(pool, with_provider, ACME, paid_id)

    ledger = (await client.get("/ledger", headers=REVIEWER)).json()

    assert ledger["available"] is True
    [line] = ledger["lines"]
    assert (line["refund_id"], line["state"], line["ok"]) == (paid_id, "matched", True)


async def test_ledger_says_when_the_provider_is_down(client, pool, refunds):
    class Down:
        async def list_refunds(self):
            from refund_harness.payments.client import PaymentError
            raise PaymentError("down")

    app.dependency_overrides[get_provider] = lambda: Down()

    assert (await client.get("/ledger", headers=REVIEWER)).json() == {"lines": [], "available": False}


async def test_ops_summary_counts_statuses_and_queues(client, pool, refunds):
    class Queues:
        def get_queue_url(self, QueueName):
            return {"QueueUrl": QueueName}

        def get_queue_attributes(self, QueueUrl, AttributeNames):
            n = 2 if QueueUrl.endswith("dlq") else 5
            return {"Attributes": {"ApproximateNumberOfMessages": str(n), "ApproximateNumberOfMessagesNotVisible": "1"}}

    app.dependency_overrides[get_sqs] = lambda: Queues()
    pending = await request(pool, ACME, "A100", A100_TOTAL_CENTS)
    await approve(pool, pending)

    summary = (await client.get("/ops/summary", headers=REVIEWER)).json()

    assert summary["by_status"] == {"approved": {"count": 1, "amount_cents": A100_TOTAL_CENTS}}
    assert summary["outbox_unsent"] == 1  # approved, the relay has not run
    assert summary["payouts_queue"] == {"waiting": 5, "in_flight": 1}
    assert summary["dead_letters"] == {"waiting": 2, "in_flight": 1}
