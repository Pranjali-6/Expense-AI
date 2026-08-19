"""Audit logging.

Records who did what. The table is append-only at the database level, so an
entry written here cannot later be edited — only erased through the explicit
account-deletion path.

``details`` is JSONB and it is tempting to put the whole change in it. It must
carry only non-sensitive context: field *names*, counts, ids, outcomes. Users
read this table on the Audit screen and can export it, so an amount recorded
here is an amount that leaves the system.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.enums import AuditAction

logger = get_logger(__name__)

#: Keys permitted in `details`. Anything else is dropped rather than trusted to
#: have been sanitised at the call site — the same allow-list discipline the
#: logging pipeline uses, for the same reason.
_ALLOWED_DETAIL_KEYS: frozenset[str] = frozenset({
    "fields_changed", "count", "reason", "bank_code", "document_type",
    "category_source", "review_status", "format", "provider", "scope",
    "previous_role", "new_role", "statement_id", "account_id", "rule_id",
    "session_id", "error_code", "outcome",
    # Added for categorisation rules. Both are closed-set values — a category
    # slug from the fixed 22, and "exact" or "contains" — so neither can carry
    # user text. The rule's *merchant pattern* is user text and is deliberately
    # absent: an audit row is rendered on screen and included in exports.
    "category", "match_type",
})


def _jsonable(value: Any) -> Any:
    """Coerce to something ``json.dumps`` accepts.

    UUIDs, datetimes and Decimals all appear naturally in this codebase and none
    of them serialise by default. Handling it here means no call site has to
    remember to stringify an id — a detail that is easy to miss and only shows
    up as a 500 on the unhappy path, which is exactly where audit entries are
    written.
    """
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


def _sanitise(details: dict[str, Any] | None) -> dict[str, Any] | None:
    if not details:
        return None
    cleaned = {
        key: _jsonable(value)
        for key, value in details.items()
        if key in _ALLOWED_DETAIL_KEYS
    }
    dropped = sorted(set(details) - set(cleaned))
    if dropped:
        cleaned["_dropped"] = dropped
    return cleaned or None


async def record(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    action: AuditAction,
    user_id: uuid.UUID | None = None,
    resource_type: str | None = None,
    resource_id: uuid.UUID | None = None,
    request_id: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    succeeded: bool = True,
    details: dict[str, Any] | None = None,
) -> None:
    """Write an audit entry on the caller's session and transaction.

    Sharing the transaction is deliberate: if the action rolls back, so does
    its audit entry. An audit trail describing changes that never happened is
    worse than none.
    """
    await session.execute(
        text(
            """
            INSERT INTO audit_logs (
                tenant_id, user_id, action, resource_type, resource_id,
                request_id, ip_address, user_agent, succeeded, details, occurred_at
            ) VALUES (
                :tenant_id, :user_id, :action, :resource_type, :resource_id,
                :request_id, CAST(:ip_address AS inet), :user_agent, :succeeded,
                CAST(:details AS jsonb), :occurred_at
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "action": str(action),
            "resource_type": resource_type,
            "resource_id": resource_id,
            "request_id": request_id,
            "ip_address": ip_address,
            "user_agent": (user_agent or "")[:255] or None,
            "succeeded": succeeded,
            "details": json.dumps(_sanitise(details)) if details else None,
            "occurred_at": datetime.now(timezone.utc),
        },
    )

    logger.info(
        "audit_recorded",
        tenant_id=str(tenant_id),
        user_id=str(user_id) if user_id else None,
        status=str(action),
        outcome="success" if succeeded else "failure",
    )
