"""Telling the user what happened while they were not looking.

A notification here is always about *their own* data, in their own tenant, read
back on their own screen — so unlike a log line it may name a category or quote
a figure. The restraint that still applies is editorial rather than legal: a
notification that says "your statement was processed" and nothing else forces a
click to find out whether it worked, so each one carries the fact that decides
what to do next.

Two rules keep the list worth reading.

**Nothing is created for the ordinary case.** A statement that imported cleanly
and reconciled exactly produces one notification; a statement that did not
reconcile produces a different one that says so. Notifying on every success
trains people to dismiss the list without reading it, and then the one that
mattered goes with it.

**Duplicates are suppressed by resource.** The nightly sweep re-runs anomaly and
budget detection over the same months, and without a guard a user would collect
a fresh "you are over budget on Food" every single night of the month.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.enums import NotificationKind

logger = get_logger(__name__)


async def create(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    kind: NotificationKind,
    title: str,
    body: str | None = None,
    resource_type: str | None = None,
    resource_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    once_per_resource: bool = True,
) -> bool:
    """Create one notification. Returns False if it was suppressed as a repeat.

    ``once_per_resource`` compares on ``(kind, resource_type, resource_id)``
    rather than on the text, so re-wording a message does not resurrect every
    notification a user has already read and dismissed.
    """
    if once_per_resource and resource_id is not None:
        existing = (
            await session.execute(
                text(
                    """
                    SELECT 1 FROM notifications
                    WHERE kind = :kind
                      AND resource_type IS NOT DISTINCT FROM :resource_type
                      AND resource_id = :resource_id
                    LIMIT 1
                    """
                ),
                {
                    "kind": str(kind),
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                },
            )
        ).one_or_none()
        if existing is not None:
            return False

    await session.execute(
        text(
            """
            INSERT INTO notifications (
                tenant_id, user_id, kind, title, body, resource_type, resource_id
            ) VALUES (
                :tenant_id, :user_id, :kind, :title, :body, :resource_type, :resource_id
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "kind": str(kind),
            "title": title,
            "body": body,
            "resource_type": resource_type,
            "resource_id": resource_id,
        },
    )
    # The event name and the kind, never the title: a title legitimately quotes
    # a category and a figure, and a log line may not.
    logger.info("notification_created", stage="notify", status=str(kind))
    return True


async def for_statement(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    statement_id: uuid.UUID,
    inserted: int,
    duplicates: int,
    review_required: int,
    reconciles: bool,
    unverifiable: bool,
    failed_error_code: str | None = None,
) -> None:
    """The one notification an import produces, chosen by what actually happened."""
    if failed_error_code is not None:
        await create(
            session,
            tenant_id=tenant_id,
            kind=NotificationKind.STATEMENT_FAILED,
            title="A statement could not be imported",
            body="Open Statement Health to see which check stopped it.",
            resource_type="statement",
            resource_id=statement_id,
        )
        return

    if not reconciles and not unverifiable:
        await create(
            session,
            tenant_id=tenant_id,
            kind=NotificationKind.RECONCILIATION_FAILED,
            title="A statement does not reconcile",
            body=(
                f"{inserted} transactions were imported and are in your ledger, but "
                "the balances do not add up. Statement Health shows the exact gap "
                "and the first row where it diverges."
            ),
            resource_type="statement",
            resource_id=statement_id,
        )
        return

    if review_required:
        await create(
            session,
            tenant_id=tenant_id,
            kind=NotificationKind.REVIEW_REQUIRED,
            title=f"{review_required} transactions need a look",
            body="They were read with low confidence in at least one dimension.",
            resource_type="statement",
            resource_id=statement_id,
        )
        return

    await create(
        session,
        tenant_id=tenant_id,
        kind=NotificationKind.STATEMENT_PROCESSED,
        title=f"{inserted} transactions imported",
        body=(
            "Reconciles exactly."
            + (f" {duplicates} rows were already in your ledger." if duplicates else "")
        ),
        resource_type="statement",
        resource_id=statement_id,
    )


async def list_for_user(
    session: AsyncSession, *, user_id: uuid.UUID, unread_only: bool = False, limit: int = 50
) -> list[dict[str, Any]]:
    clauses = ["(n.user_id = :user_id OR n.user_id IS NULL)"]
    if unread_only:
        clauses.append("n.read_at IS NULL")

    rows = (
        await session.execute(
            text(
                f"""
                SELECT n.id, n.kind, n.title, n.body, n.resource_type,
                       n.resource_id, n.read_at, n.created_at
                FROM notifications n
                WHERE {' AND '.join(clauses)}
                ORDER BY n.created_at DESC
                LIMIT :limit
                """
            ),
            {"user_id": user_id, "limit": limit},
        )
    ).all()
    return [dict(row._mapping) for row in rows]


async def unread_count(session: AsyncSession, *, user_id: uuid.UUID) -> int:
    return int(
        (
            await session.execute(
                text(
                    "SELECT count(*) FROM notifications "
                    "WHERE (user_id = :user_id OR user_id IS NULL) AND read_at IS NULL"
                ),
                {"user_id": user_id},
            )
        ).scalar_one()
    )


async def mark_read(
    session: AsyncSession, *, user_id: uuid.UUID, notification_id: uuid.UUID | None = None
) -> int:
    """Mark one notification read, or all of them. Returns how many changed."""
    clauses = ["(user_id = :user_id OR user_id IS NULL)", "read_at IS NULL"]
    params: dict[str, Any] = {"user_id": user_id}
    if notification_id is not None:
        clauses.append("id = :id")
        params["id"] = notification_id

    result = await session.execute(
        text(f"UPDATE notifications SET read_at = now() WHERE {' AND '.join(clauses)}"),
        params,
    )
    return int(result.rowcount or 0)
