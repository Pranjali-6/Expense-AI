"""One chronological spine for everything that happened.

Transactions, statement imports, budget breaches, subscription renewals and
anomalies on a single scrollable timeline. Assembled by query rather than stored
as duplicated rows for the transaction stream — a transaction is already a dated
event, and copying it into a second table would create two versions of the same
fact that can drift apart.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

#: Above this a transaction gets its own prominent entry rather than being one
#: of the day's ordinary rows.
LARGE_TRANSACTION_FLOOR = Decimal("10000.00")


async def events(
    session: AsyncSession,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 200,
    include_transactions: bool = True,
) -> list[dict[str, Any]]:
    """The merged stream, newest first."""
    params: dict[str, Any] = {"limit": limit, "floor": LARGE_TRANSACTION_FLOOR}
    window = ""
    if date_from:
        window += " AND occurred_on >= :date_from"
        params["date_from"] = date_from
    if date_to:
        window += " AND occurred_on <= :date_to"
        params["date_to"] = date_to

    transaction_source = (
        """
        SELECT t.txn_date AS occurred_on,
               CASE WHEN t.amount >= :floor THEN 'large_transaction'
                    ELSE 'transaction' END AS kind,
               COALESCE(t.merchant, 'Transaction') AS title,
               c.name AS summary,
               t.amount,
               t.id AS transaction_id,
               NULL::uuid AS statement_id,
               t.direction AS detail
        FROM transactions t
        LEFT JOIN categories c ON c.id = t.category_id
        WHERE t.is_expense
        """
        if include_transactions else
        """
        SELECT t.txn_date AS occurred_on, 'large_transaction' AS kind,
               COALESCE(t.merchant, 'Transaction') AS title,
               c.name AS summary, t.amount, t.id AS transaction_id,
               NULL::uuid AS statement_id, t.direction AS detail
        FROM transactions t
        LEFT JOIN categories c ON c.id = t.category_id
        WHERE t.is_expense AND t.amount >= :floor
        """
    )

    rows = (
        await session.execute(
            text(
                f"""
                SELECT * FROM (
                    {transaction_source}

                    UNION ALL

                    SELECT COALESCE(s.period_end, s.created_at::date) AS occurred_on,
                           'statement_import' AS kind,
                           COALESCE(a.bank_name, s.bank_code, 'Statement') AS title,
                           CASE s.trust_status
                               WHEN 'trusted' THEN 'Imported and reconciled exactly'
                               WHEN 'untrusted' THEN 'Imported but does not reconcile'
                               ELSE 'Imported; arithmetic not verified'
                           END AS summary,
                           NULL::numeric AS amount,
                           NULL::uuid AS transaction_id,
                           s.id AS statement_id,
                           s.trust_status AS detail
                    FROM statements s
                    LEFT JOIN accounts a ON a.id = s.account_id
                    WHERE s.deleted_at IS NULL

                    UNION ALL

                    SELECT an.detected_on AS occurred_on,
                           'anomaly' AS kind,
                           COALESCE(atx.merchant, ac.name, 'Unusual activity') AS title,
                           an.reason AS summary,
                           an.observed_value AS amount,
                           an.transaction_id,
                           NULL::uuid AS statement_id,
                           an.kind AS detail
                    FROM anomalies an
                    LEFT JOIN transactions atx ON atx.id = an.transaction_id
                    LEFT JOIN categories ac ON ac.id = an.category_id

                    UNION ALL

                    SELECT sub.next_expected_on AS occurred_on,
                           'subscription_renewal' AS kind,
                           sub.merchant AS title,
                           'Expected ' || sub.cadence || ' charge' AS summary,
                           sub.typical_amount AS amount,
                           NULL::uuid AS transaction_id,
                           NULL::uuid AS statement_id,
                           sub.cadence AS detail
                    FROM subscriptions sub
                    WHERE sub.status = 'active' AND sub.next_expected_on IS NOT NULL
                ) merged
                WHERE occurred_on IS NOT NULL {window}
                ORDER BY occurred_on DESC, kind
                LIMIT :limit
                """
            ),
            params,
        )
    ).all()

    return [
        {
            "occurred_on": row.occurred_on.isoformat(),
            "kind": row.kind,
            "title": row.title,
            "summary": row.summary,
            "amount": str(Decimal(str(row.amount)).quantize(Decimal("0.01")))
            if row.amount is not None else None,
            "transaction_id": str(row.transaction_id) if row.transaction_id else None,
            "statement_id": str(row.statement_id) if row.statement_id else None,
            "detail": row.detail,
        }
        for row in rows
    ]
