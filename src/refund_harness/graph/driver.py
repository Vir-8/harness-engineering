"""The Neo4j connection. Same idea as data/db.py: one place that knows where the database is and how to log in."""

import os

from neo4j import AsyncDriver, AsyncGraphDatabase

DEFAULT_URI = "bolt://localhost:7687"
DEFAULT_USER = "neo4j"
DEFAULT_PASSWORD = "refunds-dev"  # dev only, like the Postgres password; real credentials come from env/Secrets Manager


def create_graph_driver() -> AsyncDriver:
    """A driver (it keeps its own pool of connections). Close it with `await driver.close()` or `async with`."""
    return AsyncGraphDatabase.driver(
        os.environ.get("NEO4J_URI", DEFAULT_URI),
        auth=(os.environ.get("NEO4J_USER", DEFAULT_USER), os.environ.get("NEO4J_PASSWORD", DEFAULT_PASSWORD)),
    )
