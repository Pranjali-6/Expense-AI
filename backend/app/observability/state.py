"""Gauges read from the database, not accumulated in memory.

A Prometheus counter lives in one process and dies with it. For "how many
statements did we extract this hour" that is fine and it is what counters are
for. For "how many transactions are waiting for review" it is wrong twice over:
the answer is a property of the ledger rather than of any process, and a worker
restart would report it as zero.

Those facts are already recorded durably — that is the whole design — so this
module reads them back on a timer and sets gauges from what PostgreSQL and
Redis actually say. The cost is one small query set every thirty seconds; the
benefit is a dashboard that survives a deploy.

Deliberately **not** a custom Prometheus collector. A collector's ``collect()``
runs synchronously inside the scrape, and every database call in this codebase
is async — bridging that would mean either a second synchronous engine or an
event loop nested inside a scrape handler. A background refresh is the boring
option and it degrades correctly: if it stops, the gauges go stale rather than
the metrics endpoint timing out.

The counts are **cross-tenant by design and aggregate-only**, and they do not
come from a bypass. An unscoped session sees nothing under Row Level Security —
correctly — so the numbers come from ``ops_platform_counters()``, a
``SECURITY DEFINER`` function that takes no arguments and returns three
integers. There is no row, no identifier and no amount in its result: it tells
you the size of the system and nothing about anyone in it.
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import text

from app.core.logging import get_logger
from app.db.session import get_session_factory
from app.observability import metrics

logger = get_logger(__name__)

#: Long enough that the queries are free, short enough that a dashboard feels
#: live. Prometheus scrapes every 15s; refreshing faster would only add load.
REFRESH_SECONDS = 30

_QUEUES = ("default", "extract", "intelligence")


async def refresh_once() -> dict[str, Any]:
    """Read the durable state and set the gauges. Returns what it read."""
    factory = get_session_factory()
    async with factory() as session:
        row = (
            await session.execute(text("SELECT * FROM ops_platform_counters()"))
        ).one()

    metrics.review_queue_depth.set(int(row.review_queue_depth))
    metrics.ledger_transactions.set(int(row.ledger_transactions))
    metrics.untrusted_statements.set(int(row.untrusted_statements))

    depths: dict[str, int] = {}
    try:
        from app.core.redis_client import get_redis

        redis = get_redis()
        for queue in _QUEUES:
            # Celery's Redis broker stores each queue as a list keyed by name.
            depth = int(await redis.llen(queue))
            depths[queue] = depth
            metrics.celery_queue_depth.labels(queue=queue).set(depth)
    except Exception:
        # A broker blip must not take the metrics endpoint with it. The other
        # gauges are still accurate, and a missing queue depth is visibly
        # missing on a dashboard rather than silently wrong.
        logger.warning(
            "queue_depth_unavailable", component="observability", error_code="redis_error"
        )

    return {
        "review_queue_depth": int(row.review_queue_depth),
        "ledger_transactions": int(row.ledger_transactions),
        "untrusted_statements": int(row.untrusted_statements),
        "queues": depths,
    }


async def refresh_forever(stop: asyncio.Event) -> None:
    """Refresh until asked to stop. Never raises out of the loop."""
    while not stop.is_set():
        try:
            await refresh_once()
        except Exception as exc:
            logger.warning(
                "state_gauge_refresh_failed",
                component="observability",
                error_code=type(exc).__name__,
            )
        try:
            await asyncio.wait_for(stop.wait(), timeout=REFRESH_SECONDS)
        except asyncio.TimeoutError:
            continue
