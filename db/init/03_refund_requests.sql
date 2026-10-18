-- A refund the agent (or a human) has asked for. At most one live (non-rejected) request per order is
-- enforced by the partial unique index in 04_refund_unique.sql.
CREATE TABLE refund_requests (
    id           BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    merchant_id  TEXT        NOT NULL,
    order_id     TEXT        NOT NULL,
    amount_cents INTEGER     NOT NULL CHECK (amount_cents > 0),
    status       TEXT        NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'issued')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- composite foreign key: the order must exist AND belong to this merchant
    FOREIGN KEY (merchant_id, order_id) REFERENCES orders (merchant_id, id)
);
