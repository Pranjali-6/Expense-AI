"""Scheduled recomputation of the Financial Intelligence Engine's outputs.

Subscriptions, anomalies and monthly snapshots are derived facts: they are
functions of the ledger, and the ledger changes when a statement is imported or
a category is corrected. Rather than recompute them under every dashboard
request — which would make the page slow and, worse, make two people looking at
the same month see different numbers depending on when they asked — they are
rebuilt on a schedule and after every import.

**Rebuilt wholesale, never patched.** A subscription is a statement about a
merchant's whole history and an anomaly is a statement about a month; updating
either in place is how a cancelled service stays "active" forever and a
corrected transaction keeps its stale warning.

No language model is invoked anywhere in this module.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import text

from app.core.logging import bind_context, clear_context, get_logger
from app.db.session import scoped_session
from app.intelligence import anomaly, budgets, insights, recurring
from app.models.enums import NotificationKind
from app.services import notifications

from workers import runtime
from workers.celery_app import celery_app

logger = get_logger(__name__)

#: How many recent months of snapshots to rebuild. Corrections land against old
#: transactions often enough that only refreshing the current month would leave
#: last month's report quietly wrong.
SNAPSHOT_MONTHS = 3


async def _refresh_tenant(tenant_id: uuid.UUID, *, today: date | None = None) -> dict:
    from app.intelligence.analytics import previous_month

    today = today or date.today()
    month = today.replace(day=1)

    async with scoped_session(tenant_id, actor="system") as session:
        subscriptions = await recurring.refresh(session, tenant_id=tenant_id, today=today)

    months: list[date] = []
    cursor = month
    for _ in range(SNAPSHOT_MONTHS):
        months.append(cursor)
        cursor = previous_month(cursor)

    anomalies_found = 0
    notified = 0
    async with scoped_session(tenant_id, actor="system") as session:
        for period in months:
            found = await anomaly.sweep(session, tenant_id=tenant_id, month=period)
            anomalies_found += len(found)
            snapshot = await insights.build(session, period)
            await insights.persist_snapshot(
                session, tenant_id=tenant_id, insight=snapshot
            )

        notified = await _notify(session, tenant_id=tenant_id, month=month)

    return {
        "subscriptions": len(subscriptions),
        "anomalies": anomalies_found,
        "months": len(months),
        "notifications": notified,
    }


async def _notify(session, *, tenant_id: uuid.UUID, month: date) -> int:
    """Turn this month's findings into notifications, once each.

    Reads the stored rows rather than the sweep's return value, so a finding
    that was already recorded on an earlier run is deduplicated by resource id
    in ``notifications.create`` — the sweep runs nightly over the same months,
    and without that guard a user would collect the same warning every night
    until the month ended.
    """
    from sqlalchemy import text as sql

    created = 0

    rows = (
        await session.execute(
            sql(
                """
                SELECT a.id, a.reason FROM anomalies a
                WHERE a.period_month = :month
                ORDER BY a.observed_value DESC NULLS LAST
                LIMIT 5
                """
            ),
            {"month": month},
        )
    ).all()
    for row in rows:
        created += await notifications.create(
            session,
            tenant_id=tenant_id,
            kind=NotificationKind.ANOMALY_DETECTED,
            title="Something stands out",
            # The detector's own sentence, which already carries the figures it
            # fired on. Re-wording it here would be a second explanation to keep
            # in step with the first.
            body=row.reason,
            resource_type="anomaly",
            resource_id=row.id,
        )

    for budget in await budgets.progress(session, month=month):
        if budget["state"] != "exceeded":
            continue
        created += await notifications.create(
            session,
            tenant_id=tenant_id,
            kind=NotificationKind.BUDGET_BREACH,
            title=f"{budget['category_name']} is over budget",
            body=(
                f"₹{budget['spent']} spent against a ₹{budget['amount']} budget "
                f"this month."
            ),
            resource_type="budget",
            # Scoped to the month as well as the budget: exceeding the same
            # budget again in March is news, even though February already was.
            resource_id=uuid.uuid5(
                uuid.NAMESPACE_URL, f"budget/{budget['id']}/{month.isoformat()}"
            ),
        )

    return created


@celery_app.task(name="workers.tasks.intelligence.refresh_tenant", queue="intelligence")
def refresh_tenant(*, tenant_id: str) -> dict:
    """Recompute one tenant's derived intelligence."""
    clear_context()
    bind_context(tenant_id=tenant_id)
    try:
        result = runtime.run(_refresh_tenant(uuid.UUID(tenant_id)))
        logger.info(
            "intelligence_refreshed",
            stage="intelligence",
            tenant_id=tenant_id,
            count=result["subscriptions"],
            status="ok",
        )
        return result
    except Exception as exc:
        logger.error(
            "intelligence_refresh_failed",
            stage="intelligence",
            tenant_id=tenant_id,
            error_code=type(exc).__name__,
        )
        raise
    finally:
        clear_context()


async def _active_tenants() -> list[uuid.UUID]:
    """Tenants with any ledger activity.

    Read with a superuser-free session that has no tenant scope, so it can only
    see the id column it needs — the nightly sweep has no business reading
    anyone's transactions.
    """
    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT DISTINCT tenant_id FROM statements WHERE deleted_at IS NULL"
                )
            )
        ).scalars().all()
    return [uuid.UUID(str(value)) for value in rows]


@celery_app.task(name="workers.tasks.intelligence.nightly_refresh", queue="intelligence")
def nightly_refresh() -> dict:
    """Refresh every tenant. Fans out rather than looping in one task.

    One tenant's failure must not stop the rest: a per-tenant task fails alone,
    retries alone, and shows up alone in the queue dashboard.
    """
    clear_context()
    tenants = runtime.run(_active_tenants())

    for tenant_id in tenants:
        refresh_tenant.delay(tenant_id=str(tenant_id))

    logger.info("nightly_refresh_dispatched", stage="intelligence", count=len(tenants))
    return {"tenants": len(tenants)}
