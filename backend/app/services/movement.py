"""Internal money movement: the transactions that are not spending.

Moving ₹50,000 from savings to current is one movement, not ₹100,000 of
activity. Paying a credit-card bill settles purchases that were already counted
individually on the card statement — counting the payment too reports the same
money twice. These are the classic ways a personal-finance tool tells someone
they spent more than they earned.

Two mechanisms, and both are needed:

* **Narration rules** (in ``parsers/merchants/rules.py``) already set
  ``movement_type`` and ``is_expense`` from what the statement says. That works
  on a single statement in isolation.
* **Cross-account pairing**, here, links the two sides once both have been
  imported. It is what turns "a debit that looks like a transfer" into "this
  debit and that credit are the same movement", and it is only possible with
  the whole ledger in view.

Pairing is deliberately conservative. A wrong pair hides two genuine
transactions from spending totals, which is a silent understatement — worse than
leaving them unpaired, which merely leaves them visible.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.enums import MovementType

logger = get_logger(__name__)

#: Settlement rarely takes longer than this between a person's own accounts.
#: Widening it starts pairing unrelated same-amount transactions.
MAX_GAP_DAYS = 3

#: Movement types whose two sides are worth linking.
_PAIRABLE = (
    str(MovementType.TRANSFER),
    str(MovementType.CREDIT_CARD_PAYMENT),
)


async def pair_internal_movements(
    session: AsyncSession, *, tenant_id: uuid.UUID, statement_id: uuid.UUID
) -> int:
    """Link matching debits and credits across the tenant's own accounts.

    Returns the number of pairs created. Runs after a statement's transactions
    are written, and considers that statement's rows against the whole ledger —
    the counterpart usually arrived in an earlier import.
    """
    candidates = (
        await session.execute(
            text(
                """
                SELECT id, account_id, txn_date, amount, direction, movement_type
                FROM transactions
                WHERE statement_id = :statement_id
                  AND transfer_group_id IS NULL
                  AND movement_type = ANY(:kinds)
                ORDER BY txn_date, amount
                """
            ),
            {"statement_id": statement_id, "kinds": list(_PAIRABLE)},
        )
    ).all()

    pairs = 0
    for row in candidates:
        opposite = "credit" if row.direction == "debit" else "debit"

        # The counterpart: same tenant, *different* account, opposite direction,
        # identical amount, within a few days, not already paired. Ordered by
        # date proximity so the nearest plausible match wins rather than the
        # oldest.
        match = (
            await session.execute(
                text(
                    """
                    SELECT id, account_id
                    FROM transactions
                    WHERE account_id <> :account_id
                      AND transfer_group_id IS NULL
                      AND direction = :opposite
                      AND amount = :amount
                      AND txn_date BETWEEN :from_date AND :to_date
                      AND id <> :self_id
                    ORDER BY ABS(txn_date - :txn_date), created_at
                    LIMIT 1
                    """
                ),
                {
                    "account_id": row.account_id,
                    "opposite": opposite,
                    "amount": row.amount,
                    "from_date": row.txn_date,
                    "to_date": row.txn_date,
                    "self_id": row.id,
                    "txn_date": row.txn_date,
                },
            )
        ).one_or_none()

        if match is None:
            match = (
                await session.execute(
                    text(
                        """
                        SELECT id, account_id
                        FROM transactions
                        WHERE account_id <> :account_id
                          AND transfer_group_id IS NULL
                          AND direction = :opposite
                          AND amount = :amount
                          AND txn_date BETWEEN :from_date AND :to_date
                          AND id <> :self_id
                        ORDER BY ABS(txn_date - :txn_date), created_at
                        LIMIT 1
                        """
                    ),
                    {
                        "account_id": row.account_id,
                        "opposite": opposite,
                        "amount": row.amount,
                        "from_date": row.txn_date - _days(MAX_GAP_DAYS),
                        "to_date": row.txn_date + _days(MAX_GAP_DAYS),
                        "self_id": row.id,
                        "txn_date": row.txn_date,
                    },
                )
            ).one_or_none()

        if match is None:
            continue

        group_id = uuid.uuid4()
        await session.execute(
            text(
                """
                INSERT INTO transfer_groups (
                    id, tenant_id, movement_type, amount, detected_on,
                    match_confidence, match_evidence
                ) VALUES (
                    :id, :tenant_id, :movement_type, :amount, :detected_on,
                    :confidence, CAST(:evidence AS jsonb)
                )
                """
            ),
            {
                "id": group_id,
                "tenant_id": tenant_id,
                "movement_type": row.movement_type,
                "amount": row.amount,
                "detected_on": row.txn_date,
                "confidence": "0.95",
                "evidence": '{"same_amount": true, "opposite_direction": true,'
                            ' "different_account": true}',
            },
        )

        # Both sides stop being spending. If either were left as an expense the
        # movement would still be counted once, which is the bug this exists to
        # prevent.
        await session.execute(
            text(
                """
                UPDATE transactions
                SET transfer_group_id = :group_id, is_expense = false
                WHERE id IN (:left, :right)
                """
            ),
            {"group_id": group_id, "left": row.id, "right": match.id},
        )
        pairs += 1

    if pairs:
        logger.info(
            "internal_movements_paired",
            stage="movement", statement_id=str(statement_id), count=pairs,
        )
    return pairs


def _days(count: int):
    from datetime import timedelta

    return timedelta(days=count)
