-- Why a person did what they did: required when rejecting a refund, so the audit log says why money was not paid.
ALTER TABLE refund_events ADD COLUMN note TEXT;
