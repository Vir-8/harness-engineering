-- Append-only audit log: one row per state change of a refund request.
-- A status change and its event are written in one transaction: both or neither.
CREATE TABLE refund_events (
    id         BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    refund_id  BIGINT      NOT NULL REFERENCES refund_requests (id),
    event      TEXT        NOT NULL,
    actor      TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
