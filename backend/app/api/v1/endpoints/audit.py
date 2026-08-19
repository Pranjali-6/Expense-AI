"""Who did what, readable by the person it happened to.

The audit trail is not an internal artefact here — it is a user-facing screen,
which is why ``audit_logs.details`` is restricted to non-sensitive context.
That restriction is enforced when a row is written (``services/audit.py``
sanitises what it stores), not when it is read, so this endpoint can return the
column as it stands.

Read-only and unpaginated beyond a cursor: there is no route by which a caller
can edit or delete an entry, and the retention sweep is the only thing that
removes one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import text

from app.core.deps import TenantSession

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("/logs", summary="Your account's audit trail")
async def logs(
    session: TenantSession,
    action: str | None = None,
    before: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """Newest first, cursor-paginated on ``occurred_at``.

    An offset would drift under a table that is appended to constantly — page 2
    would re-show rows from page 1 every time a new entry landed between the
    requests.
    """
    clauses: list[str] = []
    params: dict[str, Any] = {"limit": limit + 1}
    if action:
        clauses.append("a.action = :action")
        params["action"] = action
    if before:
        clauses.append("a.occurred_at < :before")
        params["before"] = before

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = (
        await session.execute(
            text(
                f"""
                SELECT a.id, a.action, a.resource_type, a.resource_id,
                       a.succeeded, a.details, a.occurred_at, a.ip_address,
                       u.email AS actor_email
                FROM audit_logs a
                LEFT JOIN users u ON u.id = a.user_id
                {where}
                ORDER BY a.occurred_at DESC
                LIMIT :limit
                """
            ),
            params,
        )
    ).all()

    has_more = len(rows) > limit
    page = rows[:limit]

    return {
        "items": [
            {
                "id": str(row.id),
                "action": row.action,
                "resource_type": row.resource_type,
                "resource_id": str(row.resource_id) if row.resource_id else None,
                "succeeded": row.succeeded,
                "details": row.details,
                "occurred_at": row.occurred_at.isoformat(),
                "ip_address": str(row.ip_address) if row.ip_address else None,
                "actor_email": row.actor_email,
            }
            for row in page
        ],
        "next_before": page[-1].occurred_at.isoformat() if has_more and page else None,
    }
