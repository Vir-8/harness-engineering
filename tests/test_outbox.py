"""The outbox: approving a refund queues its payout job in the same transaction, and the relay moves it to SQS."""

import json

import pytest
from botocore.exceptions import ClientError
from helpers import A100_TOTAL_CENTS, ACME, approve, approved_refund, request
from psycopg.errors import NotNullViolation

from refund_harness.data.repository import PAYOUT_TOPIC
from refund_harness.jobs.relay import relay_once


async def outbox(pool) -> list[dict]:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT topic, payload, sent_at FROM outbox ORDER BY id")
        return await cur.fetchall()


def received(sqs, url) -> list[dict]:
    return [json.loads(m["Body"]) for m in sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10).get("Messages", [])]


async def test_approval_queues_the_payout_job(pool, refunds):
    refund_id = await approved_refund(pool)

    [row] = await outbox(pool)
    assert row["topic"] == PAYOUT_TOPIC
    assert row["payload"] == {"merchant_id": ACME, "refund_id": refund_id}
    assert row["sent_at"] is None


async def test_failed_approval_leaves_nothing_behind(pool, refunds):
    refund_id = await request(pool, ACME, "A100", A100_TOTAL_CENTS)

    # actor=None breaks the audit insert, which runs AFTER the status update
    with pytest.raises(NotNullViolation):
        await approve(pool, refund_id, actor=None)

    assert await refunds.status_of(refund_id) == "pending"  # the status update was rolled back
    assert await refunds.events_for(refund_id) == []
    assert await outbox(pool) == []


async def test_relay_sends_each_message_once(pool, refunds, sqs, payout_queues):
    refund_id = await approved_refund(pool)

    first = await relay_once(pool, sqs, PAYOUT_TOPIC, payout_queues.url)
    second = await relay_once(pool, sqs, PAYOUT_TOPIC, payout_queues.url)

    assert (first, second) == (1, 0)
    assert received(sqs, payout_queues.url) == [{"merchant_id": ACME, "refund_id": refund_id}]
    [row] = await outbox(pool)
    assert row["sent_at"] is not None


async def test_failed_send_is_retried_next_time(pool, refunds, sqs, payout_queues):
    await approved_refund(pool)
    missing_queue = payout_queues.url + "-does-not-exist"

    with pytest.raises(ClientError):  # SQS: the queue does not exist
        await relay_once(pool, sqs, PAYOUT_TOPIC, missing_queue)

    [row] = await outbox(pool)
    assert row["sent_at"] is None
    assert await relay_once(pool, sqs, PAYOUT_TOPIC, payout_queues.url) == 1
