"""Scheduled housekeeping.

Three jobs, and the shape of the third one is the interesting part.

``retention_sweep`` applies the configured windows tenant by tenant, and deletes
the stored objects belonging to the statements it removed. It knows exactly
which objects those are because it read the keys before deleting the rows that
held them, so cleanup is **precise**: a list of things to delete, not a rule for
deciding what to keep.

``reconcile_objects`` is the audit for when that goes wrong — objects in the
bucket that no statement references. **It reports by default and deletes only
when told to**, and it refuses outright when its picture of "live" looks wrong.
That is not caution for its own sake. An earlier version of this file built the
live set by enumerating tenants on an unscoped session; Row Level Security is
enabled on ``tenants``, so it enumerated none, concluded every object was an
orphan, and deleted the entire bucket. The query bug took one line to fix. The
design that let a query bug destroy data is the part that needed rethinking, so
now:

* tenants come from ``ops_active_tenants()``, which cannot silently return
  empty for the wrong reason;
* an empty live set with a non-empty bucket aborts rather than deleting;
* deleting more than a fraction of the bucket in one run aborts;
* and nothing is deleted at all unless the caller passes ``delete=True``.

Any one of those would have prevented it. Having all four is the point: a
destructive default protected by a single check is one bug away from the same
outcome.

``erase_tenant`` is the deletion path for a user who asked to be forgotten.
Objects first, then rows, for the same reason: the keys live in the rows.

None of this invokes a language model, and none of it touches a transaction.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text

from app.core.logging import bind_context, clear_context, get_logger
from app.db.session import get_session_factory, scoped_session
from app.services import retention

from workers import runtime
from workers.celery_app import celery_app

logger = get_logger(__name__)

#: A reconciliation run that wants to delete more of the bucket than this is
#: not cleaning up, it is failing. Refuse and let a human look.
MAX_ORPHAN_FRACTION = 0.25


async def _tenants() -> list[uuid.UUID]:
    """Every live tenant id, via the narrow SECURITY DEFINER function.

    Not a plain ``SELECT``: RLS is enabled on ``tenants``, so an unscoped
    session sees zero rows rather than all of them — and a maintenance job that
    reads zero tenants does not fail, it quietly does nothing. Or worse.
    """
    factory = get_session_factory()
    async with factory() as session:
        rows = (
            await session.execute(text("SELECT tenant_id FROM ops_active_tenants()"))
        ).scalars().all()
    return [uuid.UUID(str(value)) for value in rows]


async def _sweep_all() -> dict:
    from app.services.storage import delete_statement

    totals: dict[str, int] = {}
    removed_objects = 0

    for tenant_id in await _tenants():
        async with scoped_session(tenant_id, actor="system") as session:
            result = await retention.sweep(session)
        for table, count in result.deleted.items():
            totals[table] = totals.get(table, 0) + count
        for key in result.orphaned_keys:
            delete_statement(storage_key=key)
            removed_objects += 1

    return {"deleted": totals, "objects": removed_objects}


@celery_app.task(name="workers.tasks.maintenance.retention_sweep", queue="default")
def retention_sweep() -> dict:
    """Apply retention windows to every tenant, objects included."""
    clear_context()
    try:
        result = runtime.run(_sweep_all())
        logger.info(
            "retention_sweep_completed",
            stage="retention",
            count=sum(result["deleted"].values()),
            status="ok",
        )
        return result
    except Exception as exc:
        logger.error(
            "retention_sweep_failed", stage="retention", error_code=type(exc).__name__
        )
        raise
    finally:
        clear_context()


async def _reconcile(delete: bool) -> dict:
    from app.core.config import settings
    from app.core.storage import get_storage

    tenants = await _tenants()
    live: set[str] = set()
    for tenant_id in tenants:
        async with scoped_session(tenant_id, actor="system") as session:
            live.update(await retention.storage_keys(session))

    storage = get_storage()
    present = [
        item.object_name
        for item in storage.list_objects(
            settings.MINIO_BUCKET_STATEMENTS, recursive=True
        )
    ]
    orphans = [name for name in present if name not in live]

    report = {
        "tenants": len(tenants),
        "objects": len(present),
        "referenced": len(live),
        "orphans": len(orphans),
        "deleted": 0,
        "refused": None,
    }

    # Each of these describes a picture of "live" that cannot be right, and each
    # would independently have stopped the bucket being emptied.
    if not tenants:
        report["refused"] = "no_tenants_enumerated"
    elif present and not live:
        report["refused"] = "no_referenced_objects"
    elif present and len(orphans) / len(present) > MAX_ORPHAN_FRACTION:
        report["refused"] = "orphan_fraction_too_high"

    if report["refused"]:
        logger.error(
            "object_reconciliation_refused",
            stage="retention",
            error_code=str(report["refused"]),
            count=len(orphans),
        )
        return report

    if delete:
        for name in orphans:
            storage.remove_object(settings.MINIO_BUCKET_STATEMENTS, name)
        report["deleted"] = len(orphans)

    return report


@celery_app.task(name="workers.tasks.maintenance.reconcile_objects", queue="default")
def reconcile_objects(*, delete: bool = False) -> dict:
    """Compare object storage against the database. Reports; deletes on request.

    Scheduled with ``delete=False``, so the nightly run is a measurement. A
    number above zero here means the precise cleanup in ``retention_sweep``
    missed something, which is worth investigating before it is worth deleting.
    """
    clear_context()
    try:
        report = runtime.run(_reconcile(delete))
        logger.info(
            "object_reconciliation_completed",
            stage="retention",
            count=report["orphans"],
            status="deleted" if report["deleted"] else "reported",
        )
        return report
    except Exception as exc:
        logger.error(
            "object_reconciliation_failed",
            stage="retention",
            error_code=type(exc).__name__,
        )
        raise
    finally:
        clear_context()


@celery_app.task(name="workers.tasks.maintenance.erase_tenant", queue="default")
def erase_tenant(*, tenant_id: str) -> dict:
    """Erase one tenant's objects and rows. Called by the deletion endpoint.

    Objects first. If the process dies between the two halves, the survivor is
    a database row pointing at an object that is gone — recoverable and
    visible. The other order leaves a financial document in a bucket with
    nothing left that knows it exists.
    """
    clear_context()
    bind_context(tenant_id=tenant_id)
    identifier = uuid.UUID(tenant_id)

    async def _run() -> dict:
        from app.services.storage import delete_statement

        async with scoped_session(identifier, actor="system") as session:
            keys = await retention.storage_keys(session)
        for key in keys:
            delete_statement(storage_key=key)

        async with scoped_session(identifier, actor="system") as session:
            counts = await retention.erase_tenant(session, tenant_id=identifier)
        return {"objects": len(keys), **counts}

    try:
        result = runtime.run(_run())
        from app.observability import metrics

        metrics.account_deletions_total.labels(status="completed").inc()
        logger.info(
            "tenant_erasure_completed",
            stage="retention",
            tenant_id=tenant_id,
            count=result.get("objects", 0),
            status="ok",
        )
        return result
    except Exception as exc:
        from app.observability import metrics

        metrics.account_deletions_total.labels(status="failed").inc()
        logger.error(
            "tenant_erasure_failed",
            stage="retention",
            tenant_id=tenant_id,
            error_code=type(exc).__name__,
        )
        raise
    finally:
        clear_context()
