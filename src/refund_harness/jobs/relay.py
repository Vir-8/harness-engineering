"""The outbox relay: sends the messages written to the `outbox` table on to the queue, then stamps them as sent.

Why not send to SQS straight from the approve endpoint? Because "commit the approval" and "send the message" are two
systems, and a crash between them breaks one of the two:
    commit, then send:  crash in between -> approved, but no job ever exists: the refund is never paid
    send, then commit:  the worker can receive the job before the commit is visible, find the refund still pending,
                        drop the job -> again never paid
The outbox makes the job part of the approval's own transaction, and this relay moves it to SQS afterwards.

The relay sends AT LEAST once: a crash after sending but before stamping sends the row again next time. That is fine
because the payout job is idempotent (the worker re-checks the database; the provider de-duplicates by key).
"""

import asyncio
import json
import logging

import psycopg
from botocore.exceptions import BotoCoreError, ClientError
from psycopg_pool import AsyncConnectionPool

from refund_harness.data.repository import claim_unsent_outbox, mark_outbox_sent

log = logging.getLogger(__name__)


async def relay_once(pool: AsyncConnectionPool, sqs, topic: str, queue_url: str, *, batch: int = 10) -> int:
    """Send up to `batch` unsent messages of `topic` to the queue; how many were sent.

    If a send fails, the whole batch's stamps roll back and those rows are sent again next time (duplicates are fine).
    The rows are locked during the sends, but nothing else ever touches outbox rows, so that blocks no one.
    """
    async with pool.connection() as conn, conn.transaction():
        rows = await claim_unsent_outbox(conn, topic, batch)
        for row in rows:
            await asyncio.to_thread(sqs.send_message, QueueUrl=queue_url, MessageBody=json.dumps(row["payload"]))
            await mark_outbox_sent(conn, row["id"])
    return len(rows)


async def run_relay(pool: AsyncConnectionPool, sqs, topic: str, queue_url: str, *, poll_s: float = 1.0) -> None:
    """Forever: relay what is waiting; when nothing is, check again in `poll_s` seconds. Failures are retried."""
    while True:
        try:
            sent = await relay_once(pool, sqs, topic, queue_url)
        except (BotoCoreError, ClientError, psycopg.Error) as error:
            log.warning("outbox relay failed (%s: %s); retrying", type(error).__name__, error)
            sent = 0
        if sent:
            log.info("relayed %d %s message(s)", sent, topic)
        else:
            await asyncio.sleep(poll_s)
