"""The tenant boundary in the schema itself (db/init/08): an order's customer, card and address are foreign keys on
(merchant_id, id), so a row can only point at its own merchant's rows, whatever the code above it gets wrong.
"""

import pytest
from psycopg.errors import ForeignKeyViolation


async def test_order_cannot_use_another_merchants_customer(pool):
    async with pool.connection() as conn:
        with pytest.raises(ForeignKeyViolation):
            await conn.execute(
                "INSERT INTO orders (merchant_id, id, item, total_cents, status, customer_id) "
                "VALUES ('acme', 'X1', 'thing', 100, 'placed', 'gus')"  # gus is globex's customer
            )
