"""The payout worker: receive a job, pay it out, delete the message LAST. Failures retry, then go to the dead-letter queue.

Uses a throwaway queue that hides a received message for 1 s, and the fake provider in this process.
"""

import asyncio
import json

import httpx
import pytest
from helpers import A100_TOTAL_CENTS, ACME, approved_refund, payouts, request

from refund_harness.data.repository import PAYOUT_TOPIC
from refund_harness.jobs.relay import relay_once
from refund_harness.jobs.sqs import queue_counts
from refund_harness.jobs.worker import process_batch, retry_delay_s
from refund_harness.payments.client import PaymentProvider


class Crash(BaseException):
    """The worker dying. A BaseException, like KeyboardInterrupt, so the worker's `except Exception` can't catch it."""


async def crash(paid) -> None:
    raise Crash


def send_job(sqs, queues, refund_id: int, merchant_id: str = ACME) -> None:
    sqs.send_message(QueueUrl=queues.url, MessageBody=json.dumps({"merchant_id": merchant_id, "refund_id": refund_id}))


async def work(pool, provider, sqs, queues, **options) -> int:
    """One pass of the worker, without long polling, retrying failures immediately unless told otherwise."""
    return await process_batch(pool, provider, sqs, queues, wait_s=0, **{"retry_base_s": 0, **options})


@pytest.fixture
def provider_down():
    return PaymentProvider(httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503)), base_url="http://p"))


async def test_approval_to_payout_end_to_end(pool, refunds, provider, sqs, payout_queues):
    refund_id = await approved_refund(pool)  # approval writes the job to the outbox

    await relay_once(pool, sqs, PAYOUT_TOPIC, payout_queues.url)
    assert await work(pool, provider, sqs, payout_queues) == 1

    assert await refunds.status_of(refund_id) == "issued"
    assert len(await payouts(provider)) == 1
    assert queue_counts(sqs, payout_queues.url) == {"waiting": 0, "in_flight": 0}  # paid, so the message was deleted


async def test_job_with_nothing_to_do_is_deleted(pool, refunds, provider, sqs, payout_queues):
    # SQS delivers at least once, so a job for a refund that is already paid (or not payable) is normal. If such jobs were
    # not deleted, they would cycle into the dead-letter queue and raise false alarms.
    pending = await request(pool, ACME, "A100", A100_TOTAL_CENTS)  # not approved: nothing to pay
    send_job(sqs, payout_queues, pending)

    await work(pool, provider, sqs, payout_queues)

    assert await payouts(provider) == []
    assert queue_counts(sqs, payout_queues.url) == {"waiting": 0, "in_flight": 0}


async def test_failed_job_stays_queued_for_a_retry(pool, refunds, provider, provider_down, sqs, payout_queues):
    refund_id = await approved_refund(pool)
    send_job(sqs, payout_queues, refund_id)

    await work(pool, provider_down, sqs, payout_queues)
    assert await refunds.status_of(refund_id) == "approved"

    await work(pool, provider, sqs, payout_queues)  # the provider is back
    assert await refunds.status_of(refund_id) == "issued"


async def test_repeated_failure_goes_to_dead_letters(pool, refunds, provider_down, sqs, payout_queues):
    refund_id = await approved_refund(pool)
    send_job(sqs, payout_queues, refund_id)

    for _ in range(3):  # the queue allows 3 deliveries
        assert await work(pool, provider_down, sqs, payout_queues) == 1
    assert await work(pool, provider_down, sqs, payout_queues) == 0  # the 4th receive moves it instead of delivering it

    assert queue_counts(sqs, payout_queues.dlq_url)["waiting"] == 1
    assert await refunds.status_of(refund_id) == "approved"  # waiting for a person, not paid


async def test_malformed_message_goes_to_dead_letters(pool, refunds, provider, sqs, payout_queues):
    sqs.send_message(QueueUrl=payout_queues.url, MessageBody='{"refund": "seven"}')

    for _ in range(4):
        await work(pool, provider, sqs, payout_queues)

    assert queue_counts(sqs, payout_queues.dlq_url)["waiting"] == 1


async def test_crash_after_paying_never_pays_twice(
    pool, refunds, provider, sqs, payout_queues
):
    refund_id = await approved_refund(pool)
    send_job(sqs, payout_queues, refund_id)

    with pytest.raises(Crash):
        await work(pool, provider, sqs, payout_queues, after_payout=crash)
    assert queue_counts(sqs, payout_queues.url) == {"waiting": 0, "in_flight": 1}  # not deleted, just hidden

    await asyncio.sleep(1.2)  # the visibility timeout runs out: SQS assumes the worker died
    assert await work(pool, provider, sqs, payout_queues) == 1

    assert len(await payouts(provider)) == 1  # the provider replayed the first payout
    assert await refunds.status_of(refund_id) == "issued"
    assert queue_counts(sqs, payout_queues.url) == {"waiting": 0, "in_flight": 0}


def test_retry_delay_grows_up_to_a_cap():
    assert [retry_delay_s(n, base_s=2) for n in (1, 2, 3, 4)] == [2, 4, 8, 16]
    assert retry_delay_s(20, base_s=2) == 300
