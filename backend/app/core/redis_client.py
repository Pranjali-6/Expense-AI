"""Redis connection management.

Redis backs rate limiting, short-lived caches and the Celery broker. It never
holds financial records — PostgreSQL is the source of truth, and anything in
Redis must be reconstructible from it.
"""

from __future__ import annotations

from redis.asyncio import ConnectionPool, Redis

from app.core.config import settings

_pool: ConnectionPool | None = None
_client: Redis | None = None


def get_redis() -> Redis:
    global _pool, _client
    if _client is None:
        _pool = ConnectionPool.from_url(
            settings.redis_url,
            max_connections=32,
            decode_responses=True,
            health_check_interval=30,
        )
        _client = Redis(connection_pool=_pool)
    return _client


async def ping_redis() -> bool:
    try:
        return bool(await get_redis().ping())
    except Exception:
        return False


async def close_redis() -> None:
    global _pool, _client
    if _client is not None:
        await _client.aclose()
    if _pool is not None:
        await _pool.aclose()
    _client = None
    _pool = None
