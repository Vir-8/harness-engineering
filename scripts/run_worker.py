"""Runs the outbox relay and the payout worker, until Ctrl-C. Needs docker compose up and the fake provider:

    uv run uvicorn refund_harness.payments.fake_provider:app --port 8100     # another terminal
    uv run python scripts/run_worker.py
    uv run python scripts/run_worker.py --crash-after-payout                 # dies right after the first payout

Then approve a refund (POST /refunds/{id}/approve) and watch it go: outbox -> queue -> worker -> provider -> issued.
Its metrics are at http://localhost:9101/metrics (WORKER_METRICS_PORT), where Prometheus reads them.
"""

import asyncio
import logging
import os
import sys

from prometheus_client import start_http_server

from refund_harness.data.db import create_pool
from refund_harness.data.repository import PAYOUT_TOPIC
from refund_harness.jobs.relay import run_relay
from refund_harness.jobs.sqs import create_payout_queues, create_sqs_client
from refund_harness.jobs.worker import run_queue_gauges, run_worker
from refund_harness.payments.client import PaymentProvider, ProviderRefund


async def crash(paid: ProviderRefund) -> None:
    print(f"  provider paid {paid.id}... and the worker dies before recording it or deleting the message", flush=True)
    os._exit(1)


async def main(crash_after_payout: bool) -> None:
    sqs = create_sqs_client()
    queues = create_payout_queues(sqs)
    pool = await create_pool()
    provider = PaymentProvider.from_env()
    start_http_server(int(os.environ.get("WORKER_METRICS_PORT", "9101")))  # a small server thread serving /metrics
    print(f"relaying outbox -> {queues.url}\nworking on {queues.url} (dead letters: {queues.dlq_url})", flush=True)
    try:
        await asyncio.gather(
            run_relay(pool, sqs, PAYOUT_TOPIC, queues.url),
            run_worker(pool, provider, sqs, queues, after_payout=crash if crash_after_payout else None),
            run_queue_gauges(sqs, queues),
        )
    finally:
        await provider.aclose()
        await pool.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "botocore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        asyncio.run(main(crash_after_payout="--crash-after-payout" in sys.argv[1:]))
    except KeyboardInterrupt:
        pass
