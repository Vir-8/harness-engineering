"""What is on the payout queues; optionally move dead letters back for another try.

    uv run python scripts/queues.py            # counts, and the jobs sitting in the dead-letter queue
    uv run python scripts/queues.py --redrive  # move every dead letter back to the main queue (after fixing the cause)
"""

import sys

from refund_harness.jobs.sqs import (
    create_payout_queues,
    create_sqs_client,
    queue_counts,
)


def receive_all(sqs, url: str, hide_s: int) -> list[dict]:
    """Every message on a queue. Receiving hides each one for `hide_s` seconds, so none comes back twice in the loop."""
    found = []
    while batch := sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10, VisibilityTimeout=hide_s).get("Messages", []):
        found.extend(batch)
    return found


def main(redrive: bool) -> None:
    sqs = create_sqs_client()
    queues = create_payout_queues(sqs)
    for name, url in (("payouts", queues.url), ("dead letters", queues.dlq_url)):
        print(f"{name:13} {queue_counts(sqs, url)}")
    letters = receive_all(sqs, queues.dlq_url, hide_s=30 if redrive else 1)  # just looking: back in view after 1 s
    for message in letters:
        print(f"  dead letter: {message['Body']}")
        if redrive:
            # Send first, delete second: a crash in between leaves a duplicate (harmless), never a lost job.
            sqs.send_message(QueueUrl=queues.url, MessageBody=message["Body"])
            sqs.delete_message(QueueUrl=queues.dlq_url, ReceiptHandle=message["ReceiptHandle"])
    if redrive:
        print(f"moved {len(letters)} dead letter(s) back to the payouts queue")


if __name__ == "__main__":
    main(redrive="--redrive" in sys.argv[1:])
