"""The payout worker: takes jobs off the queue and pays them out, one message at a time.

For each message:  receive -> issue_refund -> DELETE the message, last.
Deleting last means a crash anywhere before it leaves the message on the queue, and SQS delivers it again once the
visibility timeout ends. So a job is never lost, but may run more than once ("at least once"). That is safe only because
issue_refund is idempotent: an already-issued refund is skipped, and a payout interrupted mid-way is replayed by the
provider through the idempotency key instead of being paid again.

A failed job is retried with a growing delay (exponential backoff). After the queue's max_receives deliveries SQS moves
it to the dead-letter queue, where it waits for a person instead of retrying forever.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from botocore.exceptions import BotoCoreError, ClientError
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from refund_harness import metrics
from refund_harness.jobs.sqs import PayoutQueues, queue_counts
from refund_harness.payments.client import PaymentProvider, ProviderRefund
from refund_harness.payments.payout import IssueOutcome, issue_refund

log = logging.getLogger(__name__)

MAX_RETRY_DELAY_S = 300


class PayoutJob(BaseModel):
    """What a message says. Only WHICH refund: the amount and status are read fresh from the database, never trusted
    from a message that may be minutes old (or forged)."""

    merchant_id: str
    refund_id: int


def job_outcome(outcome: IssueOutcome, replayed: bool) -> str:
    if outcome == IssueOutcome.NOT_ISSUABLE:
        return "nothing_to_do"
    return "replayed" if replayed else "issued"


def retry_delay_s(receives: int, base_s: float) -> int:
    """How long a failed job waits before its next attempt: base, 2 x base, 4 x base... capped."""
    return int(min(base_s * 2 ** (receives - 1), MAX_RETRY_DELAY_S))


async def process_batch(
    pool: AsyncConnectionPool,
    provider: PaymentProvider,
    sqs,
    queues: PayoutQueues,
    *,
    wait_s: int = 20,
    retry_base_s: float = 2,
    after_payout: Callable[[ProviderRefund], Awaitable[None]] | None = None,
) -> int:
    """Receive up to 10 jobs and handle each. How many were received.

    wait_s is long polling: SQS holds the request open up to that long until a message arrives, instead of the worker
    asking again and again on an empty queue.
    """
    response = await asyncio.to_thread(
        sqs.receive_message,
        QueueUrl=queues.url,
        MaxNumberOfMessages=10,
        WaitTimeSeconds=wait_s,
        MessageSystemAttributeNames=["ApproximateReceiveCount", "SentTimestamp"],
    )
    messages = response.get("Messages", [])
    for message in messages:
        receives = int(message["Attributes"]["ApproximateReceiveCount"])
        # How long it sat on the queue (SentTimestamp is milliseconds, set by SQS when the message arrived). The best
        # single sign that workers are falling behind: it grows long before anything fails.
        metrics.PAYOUT_JOB_WAIT.observe(max(time.time() - int(message["Attributes"]["SentTimestamp"]) / 1000, 0))
        try:
            job = PayoutJob.model_validate_json(message["Body"])
            result = await issue_refund(pool, provider, job.merchant_id, job.refund_id, after_payout=after_payout)
        except Exception as error:  # noqa: BLE001
            # ANY failure (provider down, database error, a malformed message) means: not done, try later.
            # Not deleting is what keeps the job; the new visibility timeout is when it comes back.
            delay = retry_delay_s(receives, retry_base_s)
            log.warning("job failed (attempt %d, %s: %s); next try in %d s", receives, type(error).__name__, error, delay)
            metrics.PAYOUT_JOBS.labels("failed").inc()
            await asyncio.to_thread(
                sqs.change_message_visibility, QueueUrl=queues.url, ReceiptHandle=message["ReceiptHandle"], VisibilityTimeout=delay
            )
            continue
        # Issued now, or nothing to do (already issued, unknown refund): either way the job is finished.
        log.info("refund %s/%s: %s %s", job.merchant_id, job.refund_id, result.outcome, "(replayed)" if result.replayed else "")
        metrics.PAYOUT_JOBS.labels(job_outcome(result.outcome, result.replayed)).inc()
        await asyncio.to_thread(sqs.delete_message, QueueUrl=queues.url, ReceiptHandle=message["ReceiptHandle"])
    return len(messages)


async def run_worker(pool: AsyncConnectionPool, provider: PaymentProvider, sqs, queues: PayoutQueues, **options) -> None:
    """Forever: wait for jobs and handle them. If the queue itself is unreachable, wait a moment and try again."""
    while True:
        try:
            await process_batch(pool, provider, sqs, queues, **options)
        except (BotoCoreError, ClientError) as error:
            log.warning("queue unreachable (%s: %s); retrying in 5 s", type(error).__name__, error)
            await asyncio.sleep(5)


def update_queue_gauges(sqs, queues: PayoutQueues) -> None:
    for name, url in (("payouts", queues.url), ("dead_letters", queues.dlq_url)):
        for state, count in queue_counts(sqs, url).items():
            metrics.QUEUE_MESSAGES.labels(name, state).set(count)


async def run_queue_gauges(sqs, queues: PayoutQueues, *, every_s: float = 10) -> None:
    """Forever: refresh the queue-depth gauges (SQS cannot push them; something has to ask)."""
    while True:
        try:
            await asyncio.to_thread(update_queue_gauges, sqs, queues)
        except (BotoCoreError, ClientError) as error:
            log.warning("could not read queue depths (%s)", type(error).__name__)
        await asyncio.sleep(every_s)
