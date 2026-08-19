"""The monthly report, assembled as structured data.

This is deliberately *data*, not prose. The AI's only possible role downstream is
turning a finished snapshot into a sentence — and if AI is off, the UI renders
the same snapshot as cards. The product does not have an AI-shaped hole in it
when the key is missing.

Nothing here calls a model, and nothing here computes a number the assistant
will later recompute differently: the snapshot *is* the source, so a figure
quoted in prose and the same figure on a chart cannot disagree.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.intelligence import analytics

ZERO = Decimal("0.00")


@dataclass(slots=True)
class MonthlyInsight:
    month: date
    summary: dict[str, Any]
    largest_category: dict[str, Any] | None = None
    fastest_growing_category: dict[str, Any] | None = None
    largest_transaction: dict[str, Any] | None = None
    top_merchants: list[dict[str, Any]] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)
    recurring_load: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "month": self.month.isoformat(),
            "summary": self.summary,
            "largest_category": self.largest_category,
            "fastest_growing_category": self.fastest_growing_category,
            "largest_transaction": self.largest_transaction,
            "top_merchants": self.top_merchants,
            "observations": self.observations,
            "recurring_load": self.recurring_load,
        }


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


async def build(session: AsyncSession, month: date) -> MonthlyInsight:
    first, last = analytics.month_bounds(month)
    summary = await analytics.monthly_summary(session, first)
    categories = await analytics.category_breakdown(session, first)
    comparison = await analytics.compare_months(
        session, analytics.previous_month(first), first
    )

    largest_category = categories[0] if categories else None

    # "Fastest growing" means the largest *rupee* increase, not the largest
    # percentage. A category that went from ₹100 to ₹400 is a 300% rise and
    # ₹300 of real money; rent going from ₹30,000 to ₹35,000 is 17% and matters
    # far more.
    growing = [
        row for row in comparison["categories"] if Decimal(row["change"]) > ZERO
    ]
    fastest = growing[0] if growing else None

    largest_transaction = (
        await session.execute(
            text(
                """
                SELECT t.id, t.merchant, t.amount, t.txn_date, t.description,
                       c.name AS category_name
                FROM transactions t
                LEFT JOIN categories c ON c.id = t.category_id
                WHERE t.is_expense AND t.txn_date BETWEEN :first AND :last
                ORDER BY t.amount DESC
                LIMIT 1
                """
            ),
            {"first": first, "last": last},
        )
    ).one_or_none()

    recurring = (
        await session.execute(
            text(
                """
                SELECT COUNT(*) AS count,
                       COALESCE(SUM(estimated_annual_cost), 0) AS annual,
                       COALESCE(SUM(
                           CASE cadence
                               WHEN 'monthly' THEN typical_amount
                               WHEN 'weekly' THEN typical_amount * 52 / 12
                               WHEN 'fortnightly' THEN typical_amount * 26 / 12
                               WHEN 'quarterly' THEN typical_amount / 3
                               WHEN 'half_yearly' THEN typical_amount / 6
                               WHEN 'annual' THEN typical_amount / 12
                               ELSE 0
                           END
                       ), 0) AS monthly_equivalent
                FROM subscriptions
                WHERE status = 'active'
                """
            )
        )
    ).one()

    insight = MonthlyInsight(
        month=first,
        summary=summary.as_dict(),
        largest_category=largest_category,
        fastest_growing_category=fastest,
        largest_transaction=(
            {
                "id": str(largest_transaction.id),
                "merchant": largest_transaction.merchant,
                "amount": str(_money(largest_transaction.amount)),
                "txn_date": largest_transaction.txn_date.isoformat(),
                "category_name": largest_transaction.category_name,
            }
            if largest_transaction else None
        ),
        top_merchants=await analytics.top_merchants(session, first, limit=5),
        recurring_load={
            "count": int(recurring.count),
            "monthly_equivalent": str(_money(recurring.monthly_equivalent)),
            "annual": str(_money(recurring.annual)),
        },
    )
    insight.observations = _observations(insight, summary, comparison)
    return insight


def _observations(
    insight: MonthlyInsight,
    summary: analytics.MonthlySummary,
    comparison: dict[str, Any],
) -> list[dict[str, Any]]:
    """Plain statements of fact, each carrying the numbers behind it.

    Rendered directly when AI is off. When AI is on these are what the model is
    given to phrase — it never sees a transaction, and it has no arithmetic left
    to do.
    """
    notes: list[dict[str, Any]] = []

    if summary.income > ZERO:
        notes.append(
            {
                "kind": "savings_rate",
                "text": (
                    f"You kept {summary.savings_rate * 100:.0f}% of what you earned "
                    f"this month."
                ),
                "values": {
                    "income": str(summary.income),
                    "net_expenses": str(summary.net_expenses),
                    "savings_rate": str(summary.savings_rate),
                },
            }
        )

    if insight.largest_category:
        notes.append(
            {
                "kind": "largest_category",
                "text": (
                    f"{insight.largest_category['name']} was your largest category, "
                    f"at {float(insight.largest_category['share']) * 100:.0f}% of "
                    "spending."
                ),
                "values": insight.largest_category,
            }
        )

    change = Decimal(comparison["expense_change"])
    if change != ZERO:
        direction = "more" if change > ZERO else "less"
        notes.append(
            {
                "kind": "month_on_month",
                "text": (
                    f"You spent ₹{abs(change):,.2f} {direction} than the month before."
                ),
                "values": {"change": str(change)},
            }
        )

    if insight.recurring_load and insight.recurring_load["count"]:
        notes.append(
            {
                "kind": "recurring_load",
                "text": (
                    f"{insight.recurring_load['count']} recurring charges are costing "
                    f"about ₹{Decimal(insight.recurring_load['monthly_equivalent']):,.2f} "
                    "a month."
                ),
                "values": insight.recurring_load,
            }
        )

    if not summary.quality.fully_trusted:
        # Said out loud rather than buried: a total that includes unreconciled
        # statements is still the best answer available, and the user should
        # know which kind of answer they are reading.
        #
        # Zero clauses are omitted rather than printed as "0 transactions came
        # from…". Stating a count of nothing is how generated text starts
        # reading as machine output, and it buries the part that is true.
        parts: list[str] = []
        if summary.quality.from_untrusted_statements:
            parts.append(
                f"{summary.quality.from_untrusted_statements} transactions came from "
                "statements that have not reconciled"
            )
        if summary.quality.awaiting_review:
            parts.append(
                f"{summary.quality.awaiting_review} transactions are awaiting review"
            )

        # Each clause stands alone, so the sentence reads correctly whether one
        # fires or both. No `.capitalize()`: it would lowercase everything after
        # the first character, including merchant names.
        notes.append(
            {
                "kind": "data_quality",
                "text": f"{', and '.join(parts)}. These figures include them.",
                "values": summary.quality.as_dict(),
            }
        )

    return notes


async def persist_snapshot(
    session: AsyncSession, *, tenant_id: uuid.UUID, insight: MonthlyInsight
) -> None:
    """Store the month's snapshot so dashboards and the assistant agree."""
    import json

    summary = insight.summary
    category_id = None
    if insight.largest_category:
        row = (
            await session.execute(
                text("SELECT id FROM categories WHERE slug = :slug"),
                {"slug": insight.largest_category["slug"]},
            )
        ).one_or_none()
        category_id = row.id if row else None

    growing_id = None
    if insight.fastest_growing_category:
        row = (
            await session.execute(
                text("SELECT id FROM categories WHERE slug = :slug"),
                {"slug": insight.fastest_growing_category["slug"]},
            )
        ).one_or_none()
        growing_id = row.id if row else None

    quality = summary["data_quality"]

    # `narrative`, `narrative_model` and `narrative_generated_at` are left null
    # here on purpose. This phase produces *data*; prose is optional and comes
    # later, from this snapshot rather than from the ledger. A null narrative is
    # what "works with AI switched off" looks like in the schema.
    await session.execute(
        text(
            """
            INSERT INTO insight_snapshots (
                tenant_id, period_month, total_expenses, total_income,
                net_cash_flow, savings_rate, largest_category_id,
                fastest_growing_category_id, largest_transaction_id,
                transaction_count, unreviewed_count, untrusted_statement_count,
                breakdown
            ) VALUES (
                :tenant_id, :month, :expenses, :income, :net, :savings,
                :largest_category, :growing_category,
                CAST(:largest_transaction AS uuid),
                :count, :unreviewed, :untrusted, CAST(:breakdown AS jsonb)
            )
            ON CONFLICT (tenant_id, period_month) DO UPDATE SET
                total_expenses = EXCLUDED.total_expenses,
                total_income = EXCLUDED.total_income,
                net_cash_flow = EXCLUDED.net_cash_flow,
                savings_rate = EXCLUDED.savings_rate,
                largest_category_id = EXCLUDED.largest_category_id,
                fastest_growing_category_id = EXCLUDED.fastest_growing_category_id,
                largest_transaction_id = EXCLUDED.largest_transaction_id,
                transaction_count = EXCLUDED.transaction_count,
                unreviewed_count = EXCLUDED.unreviewed_count,
                untrusted_statement_count = EXCLUDED.untrusted_statement_count,
                breakdown = EXCLUDED.breakdown
            """
        ),
        {
            "tenant_id": tenant_id,
            "month": insight.month,
            "expenses": Decimal(summary["net_expenses"]),
            "income": Decimal(summary["income"]),
            "net": Decimal(summary["net_cash_flow"]),
            # CONFIDENCE is NUMERIC(4,3); quantize here so what the code holds
            # and what the database stores are the same number.
            "savings": Decimal(summary["savings_rate"]).quantize(Decimal("0.001")),
            "largest_category": category_id,
            "growing_category": growing_id,
            "largest_transaction": (
                insight.largest_transaction["id"] if insight.largest_transaction else None
            ),
            "count": summary["transaction_count"],
            "unreviewed": quality["awaiting_review"],
            "untrusted": quality["from_untrusted_statements"],
            "breakdown": json.dumps(
                {
                    "observations": insight.observations,
                    "top_merchants": insight.top_merchants,
                    "recurring_load": insight.recurring_load,
                }
            ),
        },
    )
