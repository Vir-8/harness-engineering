"""Postgres connection pool. Connections are expensive to open, so we keep a few and lend them out."""

import os

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

DEFAULT_DSN = "postgresql://app:app@localhost:5433/refunds"  # dev only; real creds come from env/Secrets Manager


async def create_pool() -> AsyncConnectionPool:
    pool = AsyncConnectionPool(
        os.environ.get("DATABASE_URL", DEFAULT_DSN),
        min_size=1,
        max_size=5,
        open=False,
        kwargs={"row_factory": dict_row},  # rows come back as dicts: row["item"], not row[2]
    )
    await pool.open(wait=True)
    return pool
