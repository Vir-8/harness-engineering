-- Each merchant's own policy text, split into short chunks for search (Phase 2c).
-- merchant_id is part of every row: one merchant must never see another's policy.
-- An embedding column (pgvector) is added later, once an embeddings provider is chosen.
CREATE TABLE policy_chunks (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    merchant_id TEXT   NOT NULL REFERENCES merchants (id),
    title       TEXT   NOT NULL,
    body        TEXT   NOT NULL,
    -- keyword search: Postgres keeps this column up to date itself from title + body
    tsv         TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', title || ' ' || body)) STORED
);

-- GIN index: maps each word to the rows containing it, so keyword search does not scan the table
CREATE INDEX policy_chunks_tsv_idx ON policy_chunks USING GIN (tsv);
CREATE INDEX policy_chunks_merchant_idx ON policy_chunks (merchant_id);

INSERT INTO policy_chunks (merchant_id, title, body) VALUES
    ('acme', 'Return window',
     'Items may be returned within 30 days of delivery for a full refund. After 30 days we cannot accept returns.'),
    ('acme', 'Damaged or defective items',
     'If an item arrives damaged or defective, we will replace it or refund the full price, including shipping.'),
    ('acme', 'Shipping costs',
     'Original shipping costs are not refunded unless the item was defective or we sent the wrong product.'),
    ('acme', 'Cancellations',
     'An order that has not shipped yet can be cancelled at any time at no charge.'),
    ('acme', 'Gift cards',
     'Gift cards are final sale and cannot be refunded or exchanged for cash.'),

    ('globex', 'Return window',
     'Items may be returned within 14 days of delivery. After 14 days we can only offer store credit.'),
    ('globex', 'Damaged or defective items',
     'Products that stop working within one year are covered by our warranty and will be repaired or replaced.'),
    ('globex', 'Shipping costs',
     'Customers pay return shipping unless the return is our mistake.'),
    ('globex', 'Cancellations',
     'Orders can be cancelled until they are packed. Once packed, please wait for delivery and return the item.'),
    ('globex', 'Gift cards',
     'Gift cards never expire and cannot be refunded.');
