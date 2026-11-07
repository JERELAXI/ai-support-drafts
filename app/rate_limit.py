import logging
import time
from dataclasses import dataclass
from functools import lru_cache

import redis

from app.config import get_settings

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 60
KEY_PREFIX = "llm:rl"


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    retry_after: int  # seconds until the next window; 0 when allowed


@lru_cache
def get_redis() -> redis.Redis:
    return redis.Redis.from_url(get_settings().redis_url)


def acquire(client: redis.Redis | None = None, limit: int | None = None) -> RateLimitResult:
    """Take one slot of the global LLM limit (fixed 60 s window shared by all workers)."""
    client = client or get_redis()
    limit = limit if limit is not None else get_settings().llm_rate_limit
    now = int(time.time())
    window = now // WINDOW_SECONDS
    key = f"{KEY_PREFIX}:{window}"

    pipe = client.pipeline(transaction=True)
    pipe.incr(key)
    pipe.expire(key, WINDOW_SECONDS, nx=True)
    count, _ = pipe.execute()

    if count > limit:
        retry_after = WINDOW_SECONDS - now % WINDOW_SECONDS
        logger.info(
            "llm rate limit exceeded count=%d limit=%d retry_after=%ds", count, limit, retry_after
        )
        return RateLimitResult(allowed=False, retry_after=retry_after)
    return RateLimitResult(allowed=True, retry_after=0)
