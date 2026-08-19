"""Budgets: what you meant to spend, against what you did."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core.deps import CurrentUser, TenantSession, enforce_csrf, parse_uuid
from app.intelligence import budgets as service
from app.models.enums import AuditAction
from app.services import audit

router = APIRouter(prefix="/budgets", tags=["budgets"])


class BudgetCreate(BaseModel):
    category_slug: str
    amount: Decimal = Field(gt=0)
    period: str = "monthly"
    starts_on: date | None = None
    alert_threshold: Decimal = Field(default=Decimal("0.80"), ge=0, le=1)


class BudgetUpdate(BaseModel):
    amount: Decimal | None = Field(default=None, gt=0)
    is_active: bool | None = None
    alert_threshold: Decimal | None = Field(default=None, ge=0, le=1)


@router.get("", summary="Budgets with progress")
async def list_budgets(
    session: TenantSession, month: str | None = None
) -> list[dict[str, Any]]:
    parsed = None
    if month:
        year, number = month.split("-")
        parsed = date(int(year), int(number), 1)
    return await service.progress(session, month=parsed)


@router.post("", status_code=201, summary="Create a budget",
             dependencies=[Depends(enforce_csrf)])
async def create_budget(
    payload: BudgetCreate, session: TenantSession, current_user: CurrentUser
) -> dict[str, Any]:
    budget_id = await service.create(
        session,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        category_slug=payload.category_slug,
        amount=payload.amount,
        period=payload.period,
        starts_on=payload.starts_on,
        alert_threshold=payload.alert_threshold,
    )
    await audit.record(
        session,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        action=AuditAction.BUDGET_CHANGE,
        resource_type="budget",
        resource_id=budget_id,
        details={"status": "created"},
    )
    return {"id": str(budget_id)}


@router.patch("/{budget_id}", summary="Change a budget",
              dependencies=[Depends(enforce_csrf)])
async def update_budget(
    budget_id: str, payload: BudgetUpdate, session: TenantSession,
    current_user: CurrentUser,
) -> dict[str, str]:
    await service.update(
        session,
        budget_id=parse_uuid(budget_id),
        amount=payload.amount,
        is_active=payload.is_active,
        alert_threshold=payload.alert_threshold,
    )
    await audit.record(
        session,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        action=AuditAction.BUDGET_CHANGE,
        resource_type="budget",
        resource_id=parse_uuid(budget_id),
        details={"status": "updated"},
    )
    return {"message": "Budget updated."}


@router.delete("/{budget_id}", summary="Delete a budget",
               dependencies=[Depends(enforce_csrf)])
async def delete_budget(
    budget_id: str, session: TenantSession, current_user: CurrentUser
) -> dict[str, str]:
    await service.remove(session, budget_id=parse_uuid(budget_id))
    await audit.record(
        session,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        action=AuditAction.BUDGET_CHANGE,
        resource_type="budget",
        resource_id=parse_uuid(budget_id),
        details={"status": "deleted"},
    )
    return {"message": "Budget deleted."}

