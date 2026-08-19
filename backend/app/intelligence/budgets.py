"""Budgets: progress, burn rate and where the month is heading.

Budgets are the one place in the product where a *prediction* is shown next to a
*fact*, so the two are kept visibly distinct: ``spent`` is what happened,
``projected`` is arithmetic about what might, and the API returns both with the
projection marked unreliable early in the month rather than quietly confident.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, ValidationFailedError

ZERO = Decimal("0.00")


async def create(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    category_slug: str,
    amount: Decimal,
    period: str = "monthly",
    starts_on: date | None = None,
    alert_threshold: Decimal = Decimal("0.80"),
) -> uuid.UUID:
    if amount <= ZERO:
        raise ValidationFailedError("A budget must be greater than zero.")

    category = (
        await session.execute(
            text("SELECT id FROM categories WHERE slug = :slug"), {"slug": category_slug}
        )
    ).one_or_none()
    if category is None:
        raise ValidationFailedError(f"Unknown category {category_slug!r}.")

    budget_id = uuid.uuid4()
    await session.execute(
        text(
            """
            INSERT INTO budgets (
                id, tenant_id, category_id, created_by, amount, period,
                starts_on, alert_threshold, is_active
            ) VALUES (
                :id, :tenant_id, :category_id, :user_id, :amount, :period,
                :starts_on, :threshold, true
            )
            """
        ),
        {
            "id": budget_id,
            "tenant_id": tenant_id,
            "category_id": category.id,
            "user_id": user_id,
            "amount": amount,
            "period": period,
            "starts_on": starts_on or date.today().replace(day=1),
            "threshold": alert_threshold,
        },
    )
    return budget_id


async def update(
    session: AsyncSession, *, budget_id: uuid.UUID, amount: Decimal | None = None,
    is_active: bool | None = None, alert_threshold: Decimal | None = None,
) -> None:
    assignments = []
    params: dict[str, Any] = {"id": budget_id}
    if amount is not None:
        if amount <= ZERO:
            raise ValidationFailedError("A budget must be greater than zero.")
        assignments.append("amount = :amount")
        params["amount"] = amount
    if is_active is not None:
        assignments.append("is_active = :is_active")
        params["is_active"] = is_active
    if alert_threshold is not None:
        assignments.append("alert_threshold = :threshold")
        params["threshold"] = alert_threshold
    if not assignments:
        return

    result = await session.execute(
        text(f"UPDATE budgets SET {', '.join(assignments)} WHERE id = :id RETURNING id"),
        params,
    )
    if result.one_or_none() is None:
        raise NotFoundError("Budget not found.")


async def remove(session: AsyncSession, *, budget_id: uuid.UUID) -> None:
    result = await session.execute(
        text("DELETE FROM budgets WHERE id = :id RETURNING id"), {"id": budget_id}
    )
    if result.one_or_none() is None:
        raise NotFoundError("Budget not found.")


async def progress(
    session: AsyncSession, *, month: date | None = None, today: date | None = None
) -> list[dict[str, Any]]:
    """Every active budget with what has been spent against it."""
    from app.intelligence.analytics import month_bounds
    from app.intelligence.forecasting import project_month

    today = today or date.today()
    month = month or today.replace(day=1)
    first, last = month_bounds(month)

    rows = (
        await session.execute(
            text(
                """
                SELECT b.id, b.amount, b.period, b.alert_threshold, b.is_active,
                       c.id AS category_id, c.slug AS category_slug,
                       c.name AS category_name, c.color,
                       COALESCE((
                           SELECT SUM(t.amount) FROM transactions t
                           WHERE t.is_expense
                             AND t.category_id = b.category_id
                             AND t.txn_date BETWEEN :first AND :last
                       ), 0) AS spent
                FROM budgets b
                JOIN categories c ON c.id = b.category_id
                WHERE b.is_active
                ORDER BY b.amount DESC
                """
            ),
            {"first": first, "last": last},
        )
    ).all()

    if not rows:
        return []

    # One projection for the month, scaled per budget by its own share. Simpler
    # and more stable than projecting each category separately from a handful of
    # transactions.
    projection = await project_month(session, month=month, today=today)
    elapsed_share = (
        Decimal(projection.days_elapsed) / Decimal(projection.days_in_month)
        if projection.days_in_month else Decimal("1")
    )

    result: list[dict[str, Any]] = []
    for row in rows:
        budget = Decimal(str(row.amount)).quantize(Decimal("0.01"))
        spent = Decimal(str(row.spent)).quantize(Decimal("0.01"))
        share = (spent / budget).quantize(Decimal("0.0001")) if budget > ZERO else ZERO
        projected = (
            (spent / elapsed_share).quantize(Decimal("0.01"))
            if elapsed_share > 0 else spent
        )

        result.append(
            {
                "id": row.id,
                "category_slug": row.category_slug,
                "category_name": row.category_name,
                "color": row.color,
                "amount": str(budget),
                "spent": str(spent),
                "remaining": str((budget - spent).quantize(Decimal("0.01"))),
                "share_used": str(share),
                "projected_total": str(projected),
                "projection_reliable": projection.reliable,
                "alert_threshold": str(
                    Decimal(str(row.alert_threshold)).quantize(Decimal("0.01"))
                ),
                "state": _state(share, Decimal(str(row.alert_threshold))),
                "days_elapsed": projection.days_elapsed,
                "days_in_month": projection.days_in_month,
            }
        )
    return result


def _state(share: Decimal, threshold: Decimal) -> str:
    if share >= Decimal("1"):
        return "exceeded"
    if share >= threshold:
        return "warning"
    return "on_track"
