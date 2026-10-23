"""A per-subject request limit: at most `limit` requests per `window_s` seconds, counted in Redis.

How it counts: the first request in a window creates the counter WITH its expiry in one atomic step (SET ... EX NX), and
every request then increments it. The window is the counter's lifetime, so it resets by itself. Doing the three steps
(create, increment, read the remaining time) in one transaction means a crash between them can never leave a counter
that lives forever, and two simultaneous requests cannot both slip under the limit.

It is a FIXED window: a client can use the whole allowance at the end of one window and again at the start of the next.
That is fine for protecting a spending allowance; a smoother limit would use a sliding window or a token bucket.

If Redis is unreachable the request is ALLOWED (fail open) and the result says it was degraded: an outage of the
cache must not take the service down with it. The price is that nothing is limited while it lasts.
"""

import logging
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    retry_after_s: int  # seconds until the window ends and the count starts again
    degraded: bool = False  # Redis could not be asked, so the request was let through unchecked


class RateLimiter:
    def __init__(self, redis: Redis, *, limit: int, window_s: int, prefix: str = "rl"):
        if limit < 1 or window_s < 1:
            raise ValueError("limit and window_s must both be at least 1")
        self._redis = redis
        self.limit = limit
        self.window_s = window_s
        self._prefix = prefix

    def key(self, scope: str, subject: str) -> str:
        return f"{self._prefix}:{scope}:{subject}"

    async def check(self, scope: str, subject: str) -> RateLimitResult:
        """Count one request from `subject` (a merchant id, say) against `scope` (an endpoint name)."""
        key = self.key(scope, subject)
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.set(key, 0, ex=self.window_s, nx=True)  # only the first request of a window creates it
                pipe.incr(key)
                pipe.ttl(key)
                _, count, ttl = await pipe.execute()
            if ttl < 0:  # a counter with no expiry (made some other way) would block this subject for ever: give it one
                await self._redis.expire(key, self.window_s)
                ttl = self.window_s
        except RedisError as error:
            log.warning("rate limiter could not reach Redis (%s: %s); letting the request through", type(error).__name__, error)
            return RateLimitResult(allowed=True, limit=self.limit, remaining=self.limit, retry_after_s=0, degraded=True)
        return RateLimitResult(
            allowed=count <= self.limit,
            limit=self.limit,
            remaining=max(self.limit - count, 0),
            retry_after_s=max(ttl, 1),
        )
