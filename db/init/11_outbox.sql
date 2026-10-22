-- The outbox: messages waiting to be sent to a queue, written in the SAME transaction as the change they announce.
-- Approving a refund and "a payout job exists" then commit together or not at all. A relay process sends the
-- unsent rows (sent_at IS NULL) to SQS afterwards and stamps them; if it crashes in between, it sends them again,
-- which is harmless because every job is idempotent.
CREATE TABLE outbox (
    id         BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    topic      TEXT        NOT NULL,   -- which kind of job, e.g. 'refund-payout'; the relay maps it to a queue
    payload    JSONB       NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at    TIMESTAMPTZ             -- NULL = not sent yet
);
-- The relay only ever looks for unsent rows, oldest first: a partial index keeps that cheap as sent rows pile up.
CREATE INDEX outbox_unsent ON outbox (id) WHERE sent_at IS NULL;
