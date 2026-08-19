"""The Privacy Center: what has and has not left this system."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from app.core.deps import TenantSession
from app.services import privacy_report

router = APIRouter(prefix="/privacy", tags=["privacy"])


@router.get("/summary", summary="Privacy posture and live counters")
async def summary(session: TenantSession) -> dict[str, Any]:
    return await privacy_report.summary(session)


@router.get("/incidents", summary="Times a privacy control fired")
async def incidents(
    session: TenantSession, limit: int = Query(default=50, ge=1, le=200)
) -> list[dict[str, Any]]:
    return await privacy_report.incidents(session, limit=limit)
