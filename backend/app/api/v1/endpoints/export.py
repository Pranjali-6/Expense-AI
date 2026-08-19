"""Downloading your own ledger.

``POST`` rather than ``GET`` because the filter set is a body, not a query
string — and because an export is a recorded action. Every call writes an audit
row: taking a complete financial history out of a system is exactly the event a
user should be able to see afterwards, and the audit trail is the only place
they could.

The response streams. There is no job, no polling and no object-storage round
trip, so the file exists in one place at the end of it: the user's disk.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.core.deps import CurrentUser, TenantSession, parse_uuid, rate_limit_api
from app.models.enums import AuditAction
from app.services import audit, export

router = APIRouter(prefix="/export", tags=["export"])


class ExportRequest(BaseModel):
    """Which rows to include. The same filters the ledger screen offers."""

    model_config = ConfigDict(extra="forbid")

    date_from: date | None = None
    date_to: date | None = None
    category: str | None = Field(default=None, max_length=64)
    account_id: str | None = None
    merchant: str | None = Field(default=None, max_length=255)
    search: str | None = Field(default=None, max_length=255)
    direction: str | None = None
    review_status: str | None = None
    is_expense: bool | None = None
    min_amount: Decimal | None = None
    max_amount: Decimal | None = None


@router.post(
    "/transactions",
    summary="Export transactions as CSV, JSON or PDF",
    dependencies=[Depends(rate_limit_api)],
)
async def export_transactions(
    payload: ExportRequest,
    session: TenantSession,
    current_user: CurrentUser,
    format: str = Query(default="csv", pattern="^(csv|json|pdf)$"),
) -> StreamingResponse:
    rows = await export.gather(
        session,
        date_from=payload.date_from,
        date_to=payload.date_to,
        category_slug=payload.category,
        account_id=parse_uuid(payload.account_id, "account_id") if payload.account_id else None,
        merchant=payload.merchant,
        search=payload.search,
        direction=payload.direction,
        review_status=payload.review_status,
        is_expense=payload.is_expense,
        min_amount=payload.min_amount,
        max_amount=payload.max_amount,
    )

    body, media_type = export.render(format, rows)
    name = export.filename(format)

    # Counts and a format, never a filter value: a search term is the user's
    # own text and an audit row is read back on a screen and included in this
    # very export.
    await audit.record(
        session,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        action=AuditAction.EXPORT,
        resource_type="transactions",
        # `count`, not `rows`: the allow-list in services/audit.py names the
        # keys an audit row may carry, and an unlisted one is dropped rather
        # than stored. Using the established name keeps the entry readable.
        details={"format": format, "count": len(rows)},
    )

    return StreamingResponse(
        body,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            # The file is generated per request from live data; a cached copy
            # in a shared proxy would be one user's ledger served to another.
            "Cache-Control": "no-store",
        },
    )
