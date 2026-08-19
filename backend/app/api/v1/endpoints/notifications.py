"""What happened while you were away."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from app.core.deps import CurrentUser, TenantSession, parse_uuid
from app.services import notifications

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", summary="Your notifications, newest first")
async def list_notifications(
    session: TenantSession,
    current_user: CurrentUser,
    unread_only: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    items = await notifications.list_for_user(
        session, user_id=current_user.id, unread_only=unread_only, limit=limit
    )
    return {
        "items": [
            {
                "id": str(row["id"]),
                "kind": row["kind"],
                "title": row["title"],
                "body": row["body"],
                "resource_type": row["resource_type"],
                "resource_id": str(row["resource_id"]) if row["resource_id"] else None,
                "read_at": row["read_at"].isoformat() if row["read_at"] else None,
                "created_at": row["created_at"].isoformat(),
            }
            for row in items
        ],
        "unread": await notifications.unread_count(session, user_id=current_user.id),
    }


@router.post("/{notification_id}/read", summary="Mark one as read")
async def mark_read(
    notification_id: str, session: TenantSession, current_user: CurrentUser
) -> dict[str, int]:
    changed = await notifications.mark_read(
        session,
        user_id=current_user.id,
        notification_id=parse_uuid(notification_id, "notification_id"),
    )
    return {"marked_read": changed}


@router.post("/read-all", summary="Mark everything as read")
async def mark_all_read(
    session: TenantSession, current_user: CurrentUser
) -> dict[str, int]:
    return {"marked_read": await notifications.mark_read(session, user_id=current_user.id)}
