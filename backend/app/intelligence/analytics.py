"""Deterministic spending analytics.

**No language model is invoked anywhere in this package.** Every number the
dashboard, the insights page and the assistant report originates here, in SQL
and Python, which is what makes them auditable: each one can be reproduced by a
query a person can read, and the tests do exactly that — they recompute every
figure independently and assert the two agree.

Three rules run through all of it.

**Only ``is_expense`` rows are spending.** Transfers between your own accounts,
credit-card settlements, cash withdrawals, refunds, salary and investments are
excluded. Counting a card payment alongside the purchases it settles is how a
personal-finance tool tells someone they spent twice their income.

**Refunds are reported, not netted away silently.** A ₹500 purchase followed by
a ₹500 refund is not "₹0 spent" and it is not "₹500 spent" either — it is both
facts, so both are returned and the caller can show either.

**Money stays exact.** PostgreSQL sums NUMERIC, Python holds Decimal, the API
emits strings. No float touches a rupee at any point in the chain.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

ZERO = Decimal("0.00")

#: Movement types that are money arriving as income rather than as a return of
#: money already counted. A refund is deliberately absent: it reduces spending,
#: it does not increase earnings.
INCOME_MOVEMENTS = ("salary", "income")


def month_bounds(month: date) -> tuple[date, date]:
    """First and last day of the month containing ``month``."""
    first = month.replace(day=1)
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    return first, last


def previous_month(month: date) -> date:
    first = month.replace(day=1)
    return (first.replace(day=1) - _one_day()).replace(day=1)


def _one_day():
    from datetime import timedelta

    return timedelta(days=1)


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


async def latest_month_with_data(session: AsyncSession) -> date | None:
    """The most recent month that actually has transactions.

    Used as the default period instead of "this month", because bank statements
    arrive *after* the month they cover. On the 3rd of any month a user's most
    recent statement is last month's, so defaulting to the current month shows
    an empty dashboard to almost everyone — which reads as a broken product
    rather than as "your latest statement is not in yet".

    Returns None when there is no data at all; the caller then falls back to the
    current month and the UI shows its genuine empty state.
    """
    value = (
        await session.execute(
            text("SELECT date_trunc('month', MAX(txn_date))::date FROM transactions")
        )
    ).scalar_one_or_none()
    return value


async def default_month(session: AsyncSession) -> date:
    """This month if it has data, otherwise the latest month that does."""
    today = date.today().replace(day=1)
    latest = await latest_month_with_data(session)
    if latest is None:
        return today
    return max(latest, today) if latest >= today else latest


@dataclass(slots=True)
class DataQuality:
    """How much of this answer rests on numbers nobody verified.

    Returned with every aggregate. A total computed partly from statements that
    did not reconcile is still the best available answer, but presenting it
    without saying so would be the dishonest part.
    """

    transactions: int = 0
    from_untrusted_statements: int = 0
    awaiting_review: int = 0

    @property
    def fully_trusted(self) -> bool:
        return self.from_untrusted_statements == 0 and self.awaiting_review == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "transactions": self.transactions,
            "from_untrusted_statements": self.from_untrusted_statements,
            "awaiting_review": self.awaiting_review,
            "fully_trusted": self.fully_trusted,
        }


@dataclass(slots=True)
class MonthlySummary:
    month: date
    expenses: Decimal = ZERO
    income: Decimal = ZERO
    refunds: Decimal = ZERO
    transfers: Decimal = ZERO
    transaction_count: int = 0
    expense_transaction_count: int = 0
    quality: DataQuality = field(default_factory=DataQuality)

    @property
    def net_expenses(self) -> Decimal:
        """Spending after money that came back."""
        return (self.expenses - self.refunds).quantize(Decimal("0.01"))

    @property
    def net_cash_flow(self) -> Decimal:
        return (self.income - self.net_expenses).quantize(Decimal("0.01"))

    @property
    def savings_rate(self) -> Decimal:
        """Share of income not spent.

        Zero when there is no income — a savings *rate* against no earnings is
        not a meaningful number, and dividing anyway would report either an
        error or a spectacular but fictional result.
        """
        if self.income <= ZERO:
            return Decimal("0.0000")
        rate = (self.income - self.net_expenses) / self.income
        return max(Decimal("0.0000"), min(Decimal("1.0000"), rate)).quantize(
            Decimal("0.0001")
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "month": self.month.isoformat(),
            "expenses": str(self.expenses),
            "net_expenses": str(self.net_expenses),
            "income": str(self.income),
            "refunds": str(self.refunds),
            "transfers": str(self.transfers),
            "net_cash_flow": str(self.net_cash_flow),
            "savings_rate": str(self.savings_rate),
            "transaction_count": self.transaction_count,
            "expense_transaction_count": self.expense_transaction_count,
            "data_quality": self.quality.as_dict(),
        }


async def monthly_summary(session: AsyncSession, month: date) -> MonthlySummary:
    """Everything the dashboard's headline figures need, in one pass."""
    first, last = month_bounds(month)

    row = (
        await session.execute(
            text(
                """
                SELECT
                    COALESCE(SUM(t.amount) FILTER (WHERE t.is_expense), 0) AS expenses,
                    COALESCE(SUM(t.amount) FILTER (
                        WHERE t.movement_type = ANY(:income_movements)
                          AND t.direction = 'credit'
                    ), 0) AS income,
                    COALESCE(SUM(t.amount) FILTER (
                        WHERE t.movement_type = 'refund'
                    ), 0) AS refunds,
                    COALESCE(SUM(t.amount) FILTER (
                        WHERE t.movement_type IN ('transfer', 'credit_card_payment')
                          AND t.direction = 'debit'
                    ), 0) AS transfers,
                    COUNT(*) AS transaction_count,
                    COUNT(*) FILTER (WHERE t.is_expense) AS expense_transaction_count,
                    COUNT(*) FILTER (
                        WHERE s.trust_status IS DISTINCT FROM 'trusted'
                    ) AS untrusted,
                    COUNT(*) FILTER (
                        WHERE t.review_status = 'review_required'
                    ) AS awaiting_review
                FROM transactions t
                LEFT JOIN statements s ON s.id = t.statement_id
                WHERE t.txn_date BETWEEN :first AND :last
                """
            ),
            {"first": first, "last": last, "income_movements": list(INCOME_MOVEMENTS)},
        )
    ).one()

    return MonthlySummary(
        month=first,
        expenses=_money(row.expenses),
        income=_money(row.income),
        refunds=_money(row.refunds),
        transfers=_money(row.transfers),
        transaction_count=int(row.transaction_count),
        expense_transaction_count=int(row.expense_transaction_count),
        quality=DataQuality(
            transactions=int(row.transaction_count),
            from_untrusted_statements=int(row.untrusted),
            awaiting_review=int(row.awaiting_review),
        ),
    )


async def trend(
    session: AsyncSession, *, months: int = 12, until: date | None = None
) -> list[dict[str, Any]]:
    """Month-by-month spending and income.

    Months with no activity are returned as zeros rather than omitted: a gap in
    a chart reads as "nothing happened", and an absent point reads as "the chart
    is broken".
    """
    anchor = (until or await default_month(session)).replace(day=1)
    series: list[dict[str, Any]] = []

    cursor = anchor
    for _ in range(months):
        series.append(cursor)
        cursor = previous_month(cursor)
    series.reverse()

    first = series[0]
    last = month_bounds(series[-1])[1]

    rows = (
        await session.execute(
            text(
                """
                SELECT date_trunc('month', t.txn_date)::date AS month,
                       COALESCE(SUM(t.amount) FILTER (WHERE t.is_expense), 0) AS expenses,
                       COALESCE(SUM(t.amount) FILTER (
                           WHERE t.movement_type = ANY(:income_movements)
                             AND t.direction = 'credit'
                       ), 0) AS income,
                       COALESCE(SUM(t.amount) FILTER (
                           WHERE t.movement_type = 'refund'
                       ), 0) AS refunds,
                       COUNT(*) AS transaction_count
                FROM transactions t
                WHERE t.txn_date BETWEEN :first AND :last
                GROUP BY 1
                """
            ),
            {"first": first, "last": last, "income_movements": list(INCOME_MOVEMENTS)},
        )
    ).all()

    by_month = {row.month: row for row in rows}

    result: list[dict[str, Any]] = []
    for month in series:
        row = by_month.get(month)
        expenses = _money(row.expenses if row else 0)
        refunds = _money(row.refunds if row else 0)
        income = _money(row.income if row else 0)
        net_expenses = (expenses - refunds).quantize(Decimal("0.01"))
        result.append(
            {
                "month": month.isoformat(),
                "expenses": str(expenses),
                "net_expenses": str(net_expenses),
                "income": str(income),
                "net_cash_flow": str((income - net_expenses).quantize(Decimal("0.01"))),
                "transaction_count": int(row.transaction_count) if row else 0,
            }
        )
    return result


async def category_breakdown(
    session: AsyncSession, month: date, *, limit: int = 30
) -> list[dict[str, Any]]:
    """Spending by category for a month, largest first.

    Only ``is_expense`` rows, so the breakdown sums to the month's expense
    total. A breakdown that does not add up to the headline is worse than no
    breakdown.
    """
    first, last = month_bounds(month)
    return await category_breakdown_between(session, first, last, limit=limit)


async def category_breakdown_between(
    session: AsyncSession, first: date, last: date, *, limit: int = 30
) -> list[dict[str, Any]]:
    """The same breakdown over an arbitrary date range.

    Exists so a question about a whole year runs the *same* arithmetic as a
    question about a month. Two implementations of "spending by category" is
    two chances for the assistant and the dashboard to disagree.
    """
    rows = (
        await session.execute(
            text(
                """
                SELECT COALESCE(c.slug, 'uncategorised') AS slug,
                       COALESCE(c.name, 'Uncategorised') AS name,
                       c.color,
                       SUM(t.amount) AS total,
                       COUNT(*) AS transaction_count
                FROM transactions t
                LEFT JOIN categories c ON c.id = t.category_id
                WHERE t.is_expense
                  AND t.txn_date BETWEEN :first AND :last
                GROUP BY c.slug, c.name, c.color
                ORDER BY SUM(t.amount) DESC
                LIMIT :limit
                """
            ),
            {"first": first, "last": last, "limit": limit},
        )
    ).all()

    total = sum((_money(row.total) for row in rows), ZERO)
    return [
        {
            "slug": row.slug,
            "name": row.name,
            "color": row.color,
            "total": str(_money(row.total)),
            "transaction_count": int(row.transaction_count),
            "share": str(
                (_money(row.total) / total).quantize(Decimal("0.0001"))
                if total > ZERO else Decimal("0.0000")
            ),
        }
        for row in rows
    ]


async def daily_series(session: AsyncSession, month: date) -> list[dict[str, Any]]:
    """Spending per day, with empty days present and zero."""
    first, last = month_bounds(month)

    rows = (
        await session.execute(
            text(
                """
                SELECT t.txn_date AS day,
                       COALESCE(SUM(t.amount) FILTER (WHERE t.is_expense), 0) AS expenses,
                       COUNT(*) FILTER (WHERE t.is_expense) AS transaction_count
                FROM transactions t
                WHERE t.txn_date BETWEEN :first AND :last
                GROUP BY t.txn_date
                """
            ),
            {"first": first, "last": last},
        )
    ).all()
    by_day = {row.day: row for row in rows}

    series: list[dict[str, Any]] = []
    cursor = first
    while cursor <= last:
        row = by_day.get(cursor)
        series.append(
            {
                "day": cursor.isoformat(),
                "expenses": str(_money(row.expenses if row else 0)),
                "transaction_count": int(row.transaction_count) if row else 0,
            }
        )
        cursor = cursor + _one_day()
    return series


async def top_merchants(
    session: AsyncSession,
    month: date | None = None,
    *,
    limit: int = 10,
    between: tuple[date, date] | None = None,
) -> list[dict[str, Any]]:
    """Largest merchants by total spend.

    Scoped to ``month``, or to an explicit ``between`` range, or — with
    neither — to all time.
    """
    clauses = ["t.is_expense", "t.merchant IS NOT NULL"]
    params: dict[str, Any] = {"limit": limit}
    window = month_bounds(month) if month is not None else between
    if window is not None:
        first, last = window
        clauses.append("t.txn_date BETWEEN :first AND :last")
        params |= {"first": first, "last": last}

    rows = (
        await session.execute(
            text(
                f"""
                SELECT t.merchant,
                       SUM(t.amount) AS total,
                       COUNT(*) AS transaction_count,
                       MAX(t.txn_date) AS last_seen,
                       -- The category this merchant is most often filed under,
                       -- not an alphabetically arbitrary one.
                       mode() WITHIN GROUP (ORDER BY c.slug) AS category_slug
                FROM transactions t
                LEFT JOIN categories c ON c.id = t.category_id
                WHERE {' AND '.join(clauses)}
                GROUP BY t.merchant
                ORDER BY SUM(t.amount) DESC
                LIMIT :limit
                """
            ),
            params,
        )
    ).all()

    return [
        {
            "merchant": row.merchant,
            "total": str(_money(row.total)),
            "transaction_count": int(row.transaction_count),
            "last_seen": row.last_seen.isoformat(),
            "category_slug": row.category_slug,
            "average": str(
                (_money(row.total) / row.transaction_count).quantize(Decimal("0.01"))
            ),
        }
        for row in rows
    ]


async def compare_months(
    session: AsyncSession, left: date, right: date
) -> dict[str, Any]:
    """Two months side by side, with per-category movement.

    The delta is computed here rather than by the caller so that every surface —
    dashboard, insights, assistant — reports the same arithmetic.
    """
    left_summary = await monthly_summary(session, left)
    right_summary = await monthly_summary(session, right)

    left_categories = {row["slug"]: row for row in await category_breakdown(session, left)}
    right_categories = {row["slug"]: row for row in await category_breakdown(session, right)}

    movements = []
    for slug in sorted(set(left_categories) | set(right_categories)):
        before = _money(left_categories.get(slug, {}).get("total", 0))
        after = _money(right_categories.get(slug, {}).get("total", 0))
        movements.append(
            {
                "slug": slug,
                "name": (
                    right_categories.get(slug) or left_categories.get(slug)
                )["name"],
                "before": str(before),
                "after": str(after),
                "change": str((after - before).quantize(Decimal("0.01"))),
                # Percent change is undefined against a zero baseline. Null
                # rather than a made-up infinity or a misleading 100%.
                "percent_change": (
                    str(((after - before) / before).quantize(Decimal("0.0001")))
                    if before > ZERO else None
                ),
            }
        )

    movements.sort(key=lambda item: abs(Decimal(item["change"])), reverse=True)

    return {
        "left": left_summary.as_dict(),
        "right": right_summary.as_dict(),
        "expense_change": str(
            (right_summary.net_expenses - left_summary.net_expenses).quantize(
                Decimal("0.01")
            )
        ),
        "categories": movements,
    }
