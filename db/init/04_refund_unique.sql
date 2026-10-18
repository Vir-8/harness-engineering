CREATE UNIQUE INDEX refund_requests_one_live_per_order
ON refund_requests (merchant_id, order_id)
WHERE status <> 'rejected';