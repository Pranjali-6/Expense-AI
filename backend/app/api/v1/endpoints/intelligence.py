"""Deterministic financial intelligence.

Every figure returned by these endpoints is computed in SQL and Python. No
language model is reachable from any of them, which is what makes the numbers
auditable — each is reproducible by a query, and the test suite reproduces every
one independently.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Query

from app.assistant import narrative
from app.core.deps import TenantSession
from app.intelligence import analytics, anomaly, forecasting, insights, recurring, timeline

router = APIRouter(prefix="/intelligence", tags=["intelligence"])


def _parse_month(value: str) -> date:
    try:
        year, month = value.split("-")
        return date(int(year), int(month), 1)
    except (ValueError, TypeError):
        from app.core.errors import ValidationFailedError

        raise ValidationFailedError("month must be in YYYY-MM form.") from None


async def _month(session, value: str | None) -> date:
    """Parse ``YYYY-MM``, or fall back to the latest month with data.

    Not "the current month": statements arrive after the period they cover, so
    that default would show most users an empty dashboard.
    """
    if value:
        return _parse_month(value)
    return await analytics.default_month(session)


@router.get("/summary", summary="Headline figures for a month")
async def summary(session: TenantSession, month: str | None = None) -> dict[str, Any]:
    return (await analytics.monthly_summary(session, await _month(session, month))).as_dict()


@router.get("/trend", summary="Month-by-month spending and income")
async def trend(
    session: TenantSession, months: int = Query(default=12, ge=1, le=36)
) -> list[dict[str, Any]]:
    return await analytics.trend(session, months=months)


@router.get("/categories", summary="Spending by category")
async def categories(
    session: TenantSession, month: str | None = None
) -> list[dict[str, Any]]:
    return await analytics.category_breakdown(session, await _month(session, month))


@router.get("/daily", summary="Spending per day")
async def daily(session: TenantSession, month: str | None = None) -> list[dict[str, Any]]:
    return await analytics.daily_series(session, await _month(session, month))


@router.get("/top-merchants", summary="Where the money went")
async def top_merchants(
    session: TenantSession,
    month: str | None = None,
    limit: int = Query(default=10, ge=1, le=50),
    all_time: bool = False,
) -> list[dict[str, Any]]:
    return await analytics.top_merchants(
        session, None if all_time else await _month(session, month), limit=limit
    )


@router.get("/compare", summary="Two months side by side")
async def compare(
    session: TenantSession, left: str, right: str
) -> dict[str, Any]:
    return await analytics.compare_months(session, _parse_month(left), _parse_month(right))


@router.get("/recurring", summary="Detected subscriptions and recurring charges")
async def subscriptions(session: TenantSession) -> list[dict[str, Any]]:
    return await recurring.list_subscriptions(session)


@router.get("/anomalies", summary="Statistical outliers, with reasons")
async def anomalies(
    session: TenantSession, limit: int = Query(default=50, ge=1, le=200)
) -> list[dict[str, Any]]:
    """Outliers with stated reasons — never fraud claims."""
    return await anomaly.list_anomalies(session, limit=limit)


@router.get("/timeline", summary="One chronological stream of everything")
async def timeline_events(
    session: TenantSession,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = Query(default=200, ge=1, le=500),
    include_transactions: bool = True,
) -> list[dict[str, Any]]:
    return await timeline.events(
        session,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        include_transactions=include_transactions,
    )


@router.get("/forecast", summary="Where this month is heading")
async def forecast(session: TenantSession, month: str | None = None) -> dict[str, Any]:
    return (
        await forecasting.project_month(session, month=await _month(session, month))
    ).as_dict()


@router.get("/insights/{month}", summary="The monthly report, as structured data")
async def monthly_insight(month: str, session: TenantSession) -> dict[str, Any]:
    """The month's figures, and the phrasing of them if one exists.

    The figures are computed here, deterministically, every time. ``narrative``
    is read from the stored snapshot and is null whenever AI is off, whenever
    the nightly job has not run, and whenever a generated paragraph failed its
    checks. The screen renders the observations as cards regardless — the
    narrative is an addition to the report, never the report itself.
    """
    period = _parse_month(month)
    report = (await insights.build(session, period)).as_dict()
    report["narrative"] = await narrative.stored(session, period)
    return report
