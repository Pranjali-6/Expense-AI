"""The trusted ledger: reading it, and correcting it."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from fastapi import APIRouter, Depends, Query

from app.core.deps import CurrentUser, TenantSession, enforce_csrf, parse_uuid
from app.models.enums import AuditAction
from app.schemas.transaction import (
    ApplyToSimilarRequest,
    AuditEntry,
    BulkApproveRequest,
    ExplanationResponse,
    ReviewStats,
    TransactionCorrection,
    TransactionDetail,
    TransactionPage,
)
from app.services import audit
from app.services import transactions as service

router = APIRouter(prefix="/transactions", tags=["transactions"])


@router.get("", response_model=TransactionPage, summary="List transactions")
async def list_transactions(
    session: TenantSession,
    date_from: date | None = None,
    date_to: date | None = None,
    category: str | None = None,
    account_id: str | None = None,
    merchant: str | None = None,
    search: str | None = Query(default=None, max_length=120),
    direction: str | None = None,
    review_status: str | None = None,
    is_expense: bool | None = None,
    min_amount: Decimal | None = None,
    max_amount: Decimal | None = None,
    max_confidence: Decimal | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> TransactionPage:
    items, total = await service.list_transactions(
        session,
        date_from=date_from,
        date_to=date_to,
        category_slug=category,
        account_id=parse_uuid(account_id) if account_id else None,
        merchant=merchant,
        search=search,
        direction=direction,
        review_status=review_status,
        is_expense=is_expense,
        min_amount=min_amount,
        max_amount=max_amount,
        max_confidence=max_confidence,
        limit=limit,
        offset=offset,
    )
    return TransactionPage(items=items, total=total, limit=limit, offset=offset)


@router.get("/review/stats", response_model=ReviewStats, summary="Review queue counts")
async def review_stats(session: TenantSession) -> ReviewStats:
    return ReviewStats(**await service.review_stats(session))


@router.get("/{transaction_id}", response_model=TransactionDetail, summary="One transaction")
async def get_transaction(transaction_id: str, session: TenantSession) -> TransactionDetail:
    return TransactionDetail(
        **await service.get_transaction(session, parse_uuid(transaction_id))
    )


@router.get(
    "/{transaction_id}/explain",
    response_model=ExplanationResponse,
    summary="Why was this categorised this way?",
)
async def explain(transaction_id: str, session: TenantSession) -> ExplanationResponse:
    return ExplanationResponse(**await service.explain(session, parse_uuid(transaction_id)))


@router.get(
    "/{transaction_id}/audit",
    response_model=list[AuditEntry],
    summary="Correction history",
)
async def audit_trail(transaction_id: str, session: TenantSession) -> list[AuditEntry]:
    rows = await service.audit_trail(session, parse_uuid(transaction_id))
    return [AuditEntry(**row) for row in rows]


@router.patch(
    "/{transaction_id}",
    response_model=TransactionDetail,
    summary="Correct a transaction",
    dependencies=[Depends(enforce_csrf)],
)
async def correct(
    transaction_id: str,
    payload: TransactionCorrection,
    session: TenantSession,
    current_user: CurrentUser,
) -> TransactionDetail:
    """Write a correction.

    The original values are never touched — corrections land in ``corrected_*``
    columns and the effective value is a generated coalesce. Marking the row
    verified also puts it behind the trigger that rejects AI-sourced writes.
    """
    changes = payload.model_dump(
        exclude_unset=True,
        exclude={"category_slug", "subcategory_slug", "verify"},
    )
    result = await service.correct(
        session,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        transaction_id=parse_uuid(transaction_id),
        changes=changes,
        category_slug=payload.category_slug,
        subcategory_slug=payload.subcategory_slug,
        verify=payload.verify,
    )
    await audit.record(
        session,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        action=AuditAction.TRANSACTION_EDIT,
        resource_type="transaction",
        resource_id=parse_uuid(transaction_id),
        # Field names only. The values are the user's financial data and have no
        # business in an audit log line.
        details={"count": len(changes)},
    )
    return TransactionDetail(**result)


@router.post(
    "/bulk-approve",
    summary="Accept transactions as read",
    dependencies=[Depends(enforce_csrf)],
)
async def bulk_approve(
    payload: BulkApproveRequest, session: TenantSession, current_user: CurrentUser
) -> dict:
    approved = await service.approve(
        session, user_id=current_user.id, transaction_ids=payload.transaction_ids
    )
    await audit.record(
        session,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        action=AuditAction.TRANSACTION_APPROVE,
        resource_type="transaction",
        details={"count": approved},
    )
    return {"approved": approved}


@router.post(
    "/{transaction_id}/apply-to-similar",
    summary="Apply this category to the same merchant elsewhere",
    dependencies=[Depends(enforce_csrf)],
)
async def apply_to_similar(
    transaction_id: str,
    payload: ApplyToSimilarRequest,
    session: TenantSession,
    current_user: CurrentUser,
) -> dict:
    """Retag every other unverified transaction with the same merchant.

    Rows a human has already verified are skipped. A previous decision is never
    overwritten by a later bulk action.
    """
    updated = await service.apply_to_similar(
        session,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        transaction_id=parse_uuid(transaction_id),
        category_slug=payload.category_slug,
    )
    return {"updated": updated}

