"""Deleting data on a schedule, and on request.

Two very different obligations live here and they are kept apart on purpose.

**Retention** is routine and bounded: job events stop being useful after a few
months, audit rows and statements have statutory lifetimes measured in years,
and nothing about a sweep should ever be capable of removing a transaction. It
runs nightly, deletes in batches, and touches only tables whose retention is
configured.

**Erasure** is the opposite: a user asked to be forgotten, so *everything* goes,
including the audit trail that records them asking. It is deliberate, it is
irreversible, and it happens in one transaction so a half-erased account cannot
exist.

The audit tables refuse ``DELETE`` unless ``app.allow_audit_purge`` is set for
the transaction (migration 0004). Erasure sets it; the retention sweep sets it
too, but only for the audit table whose retention window has genuinely passed.
Both are greppable, which is the point of the flag: a delete against an audit
table without it still fails, so an accidental one cannot succeed quietly.

Object storage is emptied **before** the database rows go. Get that order wrong
and the keys are gone while the objects remain, which is the one failure mode
that produces orphaned PDFs nobody can find, let alone delete.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.observability import metrics

logger = get_logger(__name__)

#: Deleted in chunks so a sweep never holds a lock long enough to block an
#: import. A sweep that has to be killed halfway is a sweep that ran.
BATCH = 5_000


@dataclass(slots=True)
class SweepResult:
    deleted: dict[str, int] = field(default_factory=dict)
    #: Object keys belonging to the statements this sweep removed. Collected
    #: *before* the rows go, because the key lives in the row — this is what
    #: makes object cleanup precise instead of a set difference against the
    #: whole bucket.
    orphaned_keys: list[str] = field(default_factory=list)

    def add(self, table: str, count: int) -> None:
        if count:
            self.deleted[table] = self.deleted.get(table, 0) + count
            metrics.retention_deleted_total.labels(table=table).inc(count)

    @property
    def total(self) -> int:
        return sum(self.deleted.values())


async def _delete_batched(
    session: AsyncSession, *, table: str, where: str, params: dict[str, Any]
) -> int:
    """Delete matching rows a batch at a time, by primary key."""
    removed = 0
    while True:
        result = await session.execute(
            text(
                f"""
                DELETE FROM {table}
                WHERE id IN (
                    SELECT id FROM {table} WHERE {where} LIMIT :batch
                )
                """
            ),
            {**params, "batch": BATCH},
        )
        count = int(result.rowcount or 0)
        removed += count
        if count < BATCH:
            return removed


async def sweep(session: AsyncSession) -> SweepResult:
    """Apply the configured retention windows. Never touches a transaction.

    Deliberately conservative about what it is allowed to remove. Statements are
    deleted only once the user has already soft-deleted them *and* the retention
    window has passed since — the window alone is not consent, and a statement
    someone still has on screen is not stale just because it is old.
    """
    result = SweepResult()

    result.add(
        "job_events",
        await _delete_batched(
            session,
            table="job_events",
            # `occurred_at`, not `created_at`: job_events records when the
            # pipeline stage happened, and has no separate insert timestamp.
            where="occurred_at < now() - make_interval(days => :days)",
            params={"days": settings.JOB_EVENT_RETENTION_DAYS},
        ),
    )

    # Read notifications only. An unread one has not done its job yet, however
    # long it has been sitting there.
    result.add(
        "notifications",
        await _delete_batched(
            session,
            table="notifications",
            where=(
                "read_at IS NOT NULL "
                "AND read_at < now() - make_interval(days => :days)"
            ),
            params={"days": 90},
        ),
    )

    await session.execute(text("SELECT set_config('app.allow_audit_purge', 'on', true)"))
    result.add(
        "audit_logs",
        await _delete_batched(
            session,
            table="audit_logs",
            where="occurred_at < now() - make_interval(days => :days)",
            params={"days": settings.AUDIT_LOG_RETENTION_DAYS},
        ),
    )

    # Read the keys first. Once the rows are gone nothing knows where their
    # objects are, and recovering that would mean listing the whole bucket and
    # subtracting — the operation that turns one wrong assumption into an empty
    # bucket. See workers/tasks/maintenance.py.
    expiring = (
        await session.execute(
            text(
                """
                SELECT storage_key FROM statements
                WHERE deleted_at IS NOT NULL
                  AND deleted_at < now() - make_interval(days => :days)
                  AND storage_key IS NOT NULL
                """
            ),
            {"days": settings.STATEMENT_RETENTION_DAYS},
        )
    ).scalars().all()
    result.orphaned_keys.extend(str(key) for key in expiring)

    result.add(
        "statements",
        await _delete_batched(
            session,
            table="statements",
            where=(
                "deleted_at IS NOT NULL "
                "AND deleted_at < now() - make_interval(days => :days)"
            ),
            params={"days": settings.STATEMENT_RETENTION_DAYS},
        ),
    )

    logger.info(
        "retention_sweep_completed",
        stage="retention",
        count=result.total,
        status="ok",
    )
    return result


async def storage_keys(session: AsyncSession) -> list[str]:
    """Every stored object belonging to this tenant."""
    rows = (
        await session.execute(
            text("SELECT storage_key FROM statements WHERE storage_key IS NOT NULL")
        )
    ).scalars().all()
    return [str(key) for key in rows]


async def erase_tenant(session: AsyncSession, *, tenant_id: uuid.UUID) -> dict[str, int]:
    """Remove a tenant and everything belonging to it. Irreversible.

    Almost every table cascades from ``tenants``, so this is mostly one delete —
    but the two audit tables refuse it without the purge flag, and the flag is
    transaction-scoped, so it is set here rather than by the caller. A caller
    that forgot would get an error, not a partial erasure, which is the correct
    way round.
    """
    await session.execute(text("SELECT set_config('app.allow_audit_purge', 'on', true)"))

    counts: dict[str, int] = {}
    for table in ("transactions", "statements", "accounts", "audit_logs"):
        counts[table] = int(
            (
                await session.execute(text(f"SELECT count(*) FROM {table}"))
            ).scalar_one()
        )

    # One statement. Every tenant-scoped table declares ON DELETE CASCADE from
    # tenants, so listing them here would be a second copy of the schema that
    # goes stale the first time a table is added — and a table missed by that
    # list is data left behind after a user asked for erasure.
    await session.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id})

    logger.info(
        "tenant_erased", stage="retention", tenant_id=str(tenant_id), status="ok"
    )
    return counts
