CREATE EXTENSION IF NOT EXISTS vector;   -- used later for policy search

CREATE TABLE merchants (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

-- Every row belongs to one merchant (tenant). The primary key includes merchant_id,
-- so two merchants can both have an order "A100" without colliding.
CREATE TABLE orders (
    merchant_id  TEXT        NOT NULL REFERENCES merchants (id),
    id           TEXT        NOT NULL,
    item         TEXT        NOT NULL,
    total_cents  INTEGER     NOT NULL CHECK (total_cents >= 0),   -- money as integer cents, never float
    status       TEXT        NOT NULL CHECK (status IN ('placed', 'shipped', 'delivered', 'cancelled', 'refunded')),
    delivered_at TIMESTAMPTZ,
    PRIMARY KEY (merchant_id, id)
);
