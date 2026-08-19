"""Accounts, discovered from statements. The user never types one in."""

from __future__ import annotations

from fastapi import APIRouter

from app.core.deps import TenantSession
from app.schemas.transaction import AccountSummary
from app.services import accounts as service

router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.get("", response_model=list[AccountSummary], summary="List accounts")
async def list_accounts(session: TenantSession) -> list[AccountSummary]:
    return [AccountSummary(**row) for row in await service.list_accounts(session)]
