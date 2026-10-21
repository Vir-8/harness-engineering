-- Advisory notes for the human approver: what the graph says about the customer behind a refund request.
--   NULL = the check never ran (no graph configured, or the database write failed): "not checked"
--   []   = the check ran and found nothing
-- A flag is {"code": ..., "message": ...}. Flags never decide anything: a person reads them before approving.
ALTER TABLE refund_requests ADD COLUMN risk_flags JSONB;
