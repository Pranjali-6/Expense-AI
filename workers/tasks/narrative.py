"""Writing the monthly narrative, on a schedule, from stored snapshots.

Deliberately its own module rather than a step inside the intelligence refresh,
and the reason is a property worth keeping: ``workers/tasks/intelligence.py``
states that no language model is invoked anywhere in it, and that claim is only
worth making if it stays literally true. A narrative step folded in there would
quietly turn a deterministic job into a job that sometimes calls a vendor.

So the schedule has two entries. 03:30 recomputes the figures; 04:00 phrases the
ones that were computed. If the second never runs — no key, budget spent, model
unreachable — every screen still works, because the first produced everything
the screens actually read.

Runs after the refresh, not with it, so the snapshot a paragraph describes is
the one that was just written rather than yesterday's.
"""

from __future__ import annotations

import uuid
from datetime import date

from app.assistant import narrative
from app.core.config import settings
from app.core.logging import bind_context, clear_context, get_logger
from app.db.session import scoped_session
from app.intelligence.analytics import previous_month

from workers import runtime
from workers.celery_app import celery_app

logger = get_logger(__name__)

#: How many recent months to phrase. Matches the snapshot refresh window, minus
#: the months nobody looks at: a narrative for a month whose figures have long
#: settled is a rupee spent on prose nobody will read.
NARRATIVE_MONTHS = 2


async def _write(tenant_id: uuid.UUID, *, today: date | None = None) -> int:
    if not settings.ai_usable:
        return 0

    anchor = (today or date.today()).replace(day=1)
    months = [anchor]
    for _ in range(NARRATIVE_MONTHS - 1):
        months.append(previous_month(months[-1]))

    written = 0
    async with scoped_session(tenant_id, actor="system") as session:
        for month in months:
            if await narrative.generate(session, tenant_id=tenant_id, month=month):
                written += 1
    return written


@celery_app.task(name="workers.tasks.narrative.write_for_tenant", queue="default")
def write_for_tenant(*, tenant_id: str) -> dict:
    """Phrase one tenant's recent snapshots."""
    clear_context()
    bind_context(tenant_id=tenant_id)
    try:
        written = runtime.run(_write(uuid.UUID(tenant_id)))
        logger.info(
            "narratives_written",
            stage="narrative",
            tenant_id=tenant_id,
            count=written,
            status="ok",
        )
        return {"written": written}
    except Exception as exc:
        logger.error(
            "narrative_write_failed",
            stage="narrative",
            tenant_id=tenant_id,
            error_code=type(exc).__name__,
        )
        raise
    finally:
        clear_context()


@celery_app.task(name="workers.tasks.narrative.nightly_narratives", queue="default")
def nightly_narratives() -> dict:
    """Fan out to every tenant, or do nothing at all with AI off.

    The early return is not an optimisation. With no key configured this task
    should leave no trace — no per-tenant tasks queued, no sessions opened, no
    log line implying work happened.
    """
    clear_context()
    if not settings.ai_usable:
        logger.info("narratives_skipped", stage="narrative", status="ai_disabled")
        return {"tenants": 0, "skipped": True}

    from workers.tasks.intelligence import _active_tenants

    tenants = runtime.run(_active_tenants())

    for tenant_id in tenants:
        write_for_tenant.delay(tenant_id=str(tenant_id))

    logger.info("narratives_dispatched", stage="narrative", count=len(tenants))
    return {"tenants": len(tenants), "skipped": False}
