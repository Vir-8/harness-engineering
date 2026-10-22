-- The payment provider's id for the money it sent back, set when a refund becomes 'issued'.
-- It is how a person (or a reconciliation job) finds the payout at the provider for a given refund request.
ALTER TABLE refund_requests ADD COLUMN provider_refund_id TEXT;
