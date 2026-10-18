-- Append-only audit log: one row per state change of a refund request.
-- Approving a refund must write the status change AND the event, or neither (Phase 2b, transactions).
CREATE TABLE refund_events (
    id         BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    refund_id  BIGINT      NOT NULL REFERENCES refund_requests (id),
    event      TEXT        NOT NULL,
    actor      TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
