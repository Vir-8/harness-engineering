#!/usr/bin/env bash
# Creates (or recreates) refunds_test: a copy of the dev database (schema, seed data, the real policy vectors) for the
# tests. They empty and rewrite tables, and the running worker would pick up what they write in the dev database.
#   scripts/create_test_db.sh        # run again after a schema change (a new db/init file)
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose exec -T db psql -q -U app -d postgres -c "DROP DATABASE IF EXISTS refunds_test" -c "CREATE DATABASE refunds_test"
docker compose exec -T db sh -c "pg_dump -U app refunds | psql -q -U app -d refunds_test" >/dev/null
# Without the demo orders of scripts/seed_demo.py (C1xx, G1xx) and the customers, cards and addresses only they use:
# the tests expect exactly the seed data of db/init.
docker compose exec -T db psql -q -U app -d refunds_test -v ON_ERROR_STOP=1 <<'SQL' >/dev/null
BEGIN;
TRUNCATE refund_requests, refund_events, outbox;
DELETE FROM orders WHERE id ~ '^(C1|G1)[0-9]{2}$';
DELETE FROM customers c WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.merchant_id = c.merchant_id AND o.customer_id = c.id);
DELETE FROM payment_methods p WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.merchant_id = p.merchant_id AND o.payment_method_id = p.id);
DELETE FROM addresses a WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.merchant_id = a.merchant_id AND o.shipping_address_id = a.id);
COMMIT;
SQL
docker compose exec -T db psql -U app -d refunds_test -qtAc "SELECT 'refunds_test ready: ' || count(*) || ' orders' FROM orders"
