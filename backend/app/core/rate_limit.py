"""Redis-backed sliding-window rate limiting.

A sliding window rather than a fixed one. Fixed windows have a boundary
problem: with a 10-per-minute limit, an attacker gets 10 requests at 11:59:59
and 10 more at 12:00:00 — twenty in a second, entirely within the rules. For
login throttling that difference matters.

Implemented as a sorted set of request timestamps per key, trimmed on each
call. Costs one round trip via a pipeline.

Fails **open** on a Redis outage: rate limiting is an availability control, and
locking every user out of their own financial records because a cache is down
is the worse failure. Authentication, authorization and tenant isolation all
fail closed, and none of them depend on this.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum

from app.core.config import settings
from app.core.logging import get_logger
from app.core.redis_client import get_redis

logger = get_logger(__name__)


class RateLimitScope(StrEnum):
    LOGIN = "login"
    REGISTER = "register"
    REFRESH = "refresh"
    API = "api"
    UPLOAD = "upload"
    ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class RateLimitRule:
    limit: int
    window_seconds: int


def _rules() -> dict[RateLimitScope, RateLimitRule]:
    return {
        # Deliberately tighter than the general API limit. Credential stuffing
        # is a volume attack, and 5/minute makes it uneconomic while staying
        # invisible to someone mistyping their own password.
        RateLimitScope.LOGIN: RateLimitRule(settings.RATE_LIMIT_AUTH_PER_MINUTE // 2 or 5, 60),
        RateLimitScope.REGISTER: RateLimitRule(settings.RATE_LIMIT_AUTH_PER_MINUTE, 3600),
        RateLimitScope.REFRESH: RateLimitRule(settings.RATE_LIMIT_AUTH_PER_MINUTE * 3, 60),
        RateLimitScope.API: RateLimitRule(settings.RATE_LIMIT_API_PER_MINUTE, 60),
        RateLimitScope.UPLOAD: RateLimitRule(settings.RATE_LIMIT_UPLOAD_PER_HOUR, 3600),
        RateLimitScope.ASSISTANT: RateLimitRule(settings.RATE_LIMIT_ASSISTANT_PER_MINUTE, 60),
    }


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    allowed: bool
    remaining: int
    retry_after_seconds: int


async def check_rate_limit(
    scope: RateLimitScope, identifier: str, *, cost: int = 1
) -> RateLimitResult:
    """Consume ``cost`` from the window for ``identifier``.

    ``identifier`` is hashed into the key by the caller when it is sensitive —
    an IP address is fine in Redis, an email address is not.
    """
    rule = _rules()[scope]

    if not settings.RATE_LIMIT_ENABLED:
        return RateLimitResult(True, rule.limit, 0)

    key = f"rl:{scope}:{identifier}"
    now_ms = int(time.time() * 1000)
    window_ms = rule.window_seconds * 1000
    cutoff = now_ms - window_ms

    try:
        redis = get_redis()
        pipe = redis.pipeline()
        pipe.zremrangebyscore(key, 0, cutoff)
        pipe.zcard(key)
        # A unique member per request; the score is the timestamp.
        for offset in range(cost):
            pipe.zadd(key, {f"{now_ms}-{offset}-{time.monotonic_ns()}": now_ms})
        pipe.expire(key, rule.window_seconds + 1)
        results = await pipe.execute()
        used_before = int(results[1])
    except Exception:
        # Fail open. See the module docstring.
        logger.warning("rate_limit_unavailable", component="rate_limit", error_code="redis_error")
        return RateLimitResult(True, rule.limit, 0)

    used = used_before + cost
    if used > rule.limit:
        return RateLimitResult(False, 0, rule.window_seconds)

    return RateLimitResult(True, max(rule.limit - used, 0), 0)


async def reset_rate_limit(scope: RateLimitScope, identifier: str) -> None:
    """Clear a window — called after a successful login so one bad day of
    typos does not lock someone out once they have proven who they are."""
    try:
        await get_redis().delete(f"rl:{scope}:{identifier}")
    except Exception:
        pass
