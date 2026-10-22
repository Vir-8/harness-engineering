"""The Redis connection. Same idea as data/db.py and graph/driver.py: one place that knows where Redis is."""

import os

import redis.asyncio as aredis

DEFAULT_URL = "redis://localhost:6379/0"  # dev only, like the other defaults
DEFAULT_TIMEOUT_S = 0.1  # a cache that takes longer than this to answer is slower than the work it saves


def create_redis(url: str | None = None, *, timeout: float = DEFAULT_TIMEOUT_S) -> aredis.Redis:
    """A client (it keeps its own pool of connections and connects lazily, so Redis being down cannot stop startup).

    Both timeouts are short on purpose: callers treat any Redis failure as "no cache", so a slow Redis must not make
    requests slow. Values are bytes (no decode_responses): the cache stores raw vector bytes.
    """
    return aredis.Redis.from_url(
        url or os.environ.get("REDIS_URL", DEFAULT_URL), socket_timeout=timeout, socket_connect_timeout=timeout
    )
