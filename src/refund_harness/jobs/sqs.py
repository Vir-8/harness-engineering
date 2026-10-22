"""The queue: Amazon SQS on AWS, ElasticMQ (same API) locally. One place that knows where it is and how queues are set up.

boto3, the AWS library, is synchronous: async code calls it through asyncio.to_thread so the event loop never blocks.
"""

import json
import os
from dataclasses import dataclass

import boto3

DEFAULT_ENDPOINT = "http://localhost:9324"  # ElasticMQ from docker compose
PAYOUTS_QUEUE = "refund-payouts"


def dead_letter_queue_name(name: str) -> str:
    return f"{name}-dlq"


def create_sqs_client():
    """A boto3 SQS client. SQS_ENDPOINT picks the server; set it to "" to use real AWS (region/credentials from the env)."""
    endpoint = os.environ.get("SQS_ENDPOINT", DEFAULT_ENDPOINT)
    if not endpoint:
        return boto3.client("sqs")
    # The emulator ignores credentials, but boto3 refuses to send a request without some.
    return boto3.client(
        "sqs", endpoint_url=endpoint, region_name="us-east-1", aws_access_key_id="local", aws_secret_access_key="local"
    )


@dataclass(frozen=True)
class PayoutQueues:
    url: str  # where payout jobs go
    dlq_url: str  # the dead-letter queue: jobs that failed `max_receives` times, waiting for a person


def create_payout_queues(sqs, name: str = PAYOUTS_QUEUE, *, visibility_timeout_s: int = 30, max_receives: int = 3) -> PayoutQueues:
    """Create (or update) the payouts queue and its dead-letter queue. Safe to run again.

    visibility_timeout_s: how long a received message stays hidden from other workers. If the worker has not deleted it
    by then, SQS assumes the worker died and delivers it again. Must be longer than the slowest payout.
    max_receives: after this many deliveries without a delete, SQS moves the message to the dead-letter queue.
    """
    dlq_url = sqs.create_queue(QueueName=dead_letter_queue_name(name))["QueueUrl"]
    dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    url = sqs.create_queue(QueueName=name)["QueueUrl"]
    # Set separately from create_queue: on AWS, create_queue with different attributes than an existing queue fails.
    sqs.set_queue_attributes(
        QueueUrl=url,
        Attributes={
            "VisibilityTimeout": str(visibility_timeout_s),
            "RedrivePolicy": json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": str(max_receives)}),
        },
    )
    return PayoutQueues(url, dlq_url)


def queue_counts(sqs, url: str) -> dict[str, int]:
    """waiting = can be received now; in_flight = received by a worker and not yet deleted (hidden for now)."""
    attributes = sqs.get_queue_attributes(
        QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"]
    )["Attributes"]
    return {
        "waiting": int(attributes["ApproximateNumberOfMessages"]),
        "in_flight": int(attributes["ApproximateNumberOfMessagesNotVisible"]),
    }
