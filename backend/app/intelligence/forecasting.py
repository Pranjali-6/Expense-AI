"""Run-rate projection. No ML, no model, no pretence of one.

A month-end projection from elapsed days is a *simple* answer, and simple is the
right kind of answer here: it is explainable in one sentence, it degrades
predictably, and a user can check it. Anything fancier would have to be
justified by accuracy nobody has measured.

The one refinement worth making is recurring charges. Straight-line projection
from the first ten days of a month misses the rent due on the 28th entirely, and
that is not a small error — so known upcoming charges are added on top of the
run rate rather than assumed to be inside it.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

ZERO = Decimal("0.00")


@dataclass(slots=True)
class Projection:
    month: date
    spent_so_far: Decimal
    days_elapsed: int
    days_in_month: int
    run_rate_projection: Decimal
    upcoming_recurring: Decimal
    projected_total: Decimal
    #: False before enough of the month has passed for a rate to mean anything.
    reliable: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "month": self.month.isoformat(),
            "spent_so_far": str(self.spent_so_far),
            "days_elapsed": self.days_elapsed,
            "days_in_month": self.days_in_month,
            "run_rate_projection": str(self.run_rate_projection),
            "upcoming_recurring": str(self.upcoming_recurring),
            "projected_total": str(self.projected_total),
            "reliable": self.reliable,
        }


#: Below this many elapsed days a run rate is dominated by whatever happened to
#: fall in the first few days. The projection is still returned — with
#: `reliable: false`, so the UI can show it as an early estimate rather than
#: hiding a number the user can see on the page anyway.
MIN_DAYS_FOR_CONFIDENCE = 7


async def project_month(
    session: AsyncSession, *, month: date, today: date | None = None
) -> Projection:
    from app.intelligence.analytics import month_bounds

    today = today or date.today()
    first, last = month_bounds(month)
    days_in_month = calendar.monthrange(first.year, first.month)[1]

    # A month in the past is complete: its "projection" is what happened.
    as_of = min(today, last)
    days_elapsed = max((as_of - first).days + 1, 1) if as_of >= first else 0

    spent = Decimal(
        str(
            (
                await session.execute(
                    text(
                        "SELECT COALESCE(SUM(amount), 0) FROM transactions "
                        "WHERE is_expense AND txn_date BETWEEN :first AND :as_of"
                    ),
                    {"first": first, "as_of": as_of},
                )
            ).scalar_one()
        )
    ).quantize(Decimal("0.01"))

    if days_elapsed >= days_in_month:
        return Projection(
            month=first, spent_so_far=spent, days_elapsed=days_in_month,
            days_in_month=days_in_month, run_rate_projection=spent,
            upcoming_recurring=ZERO, projected_total=spent, reliable=True,
        )

    daily_rate = spent / days_elapsed if days_elapsed else ZERO
    run_rate = (daily_rate * days_in_month).quantize(Decimal("0.01"))

    # Known charges still to fall this month. Added on top of the run rate
    # rather than folded into it: they have not happened yet, so they are not
    # in the rate.
    upcoming = Decimal(
        str(
            (
                await session.execute(
                    text(
                        """
                        SELECT COALESCE(SUM(typical_amount), 0)
                        FROM subscriptions
                        WHERE status = 'active'
                          AND next_expected_on IS NOT NULL
                          AND next_expected_on > :as_of
                          AND next_expected_on <= :last
                        """
                    ),
                    {"as_of": as_of, "last": last},
                )
            ).scalar_one()
        )
    ).quantize(Decimal("0.01"))

    # The run rate already extrapolates the *average* day, which includes days
    # a subscription happened to fall on. Taking the larger of the two rather
    # than the sum avoids counting a recurring charge twice.
    projected = max(run_rate, spent + upcoming).quantize(Decimal("0.01"))

    return Projection(
        month=first,
        spent_so_far=spent,
        days_elapsed=days_elapsed,
        days_in_month=days_in_month,
        run_rate_projection=run_rate,
        upcoming_recurring=upcoming,
        projected_total=projected,
        reliable=days_elapsed >= MIN_DAYS_FOR_CONFIDENCE,
    )
