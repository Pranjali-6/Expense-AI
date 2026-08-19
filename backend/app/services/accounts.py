"""Accounts, discovered from statements rather than typed in.

The user never enters an account. A statement carries a bank, a document type
and the last four digits of the account or card, and that triple is enough to
tell one person's accounts apart while storing nothing worth stealing — there is
no full account number anywhere in this system.

``account_fingerprint`` is an HMAC of that triple under the deployment's master
key. It is the uniqueness key rather than the plain last four digits so that a
leaked row cannot be matched back to an account by anyone who can guess a bank
and four digits, which is a very small search space.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import account_fingerprint
from app.models.enums import AccountType, DocumentType

#: Used when a statement did not print an identifiable account number. Grouping
#: those under one placeholder per bank is better than creating a new account
#: per import, which would fragment a person's history invisibly.
UNKNOWN_LAST4 = "0000"


def account_type_for(document_type: DocumentType, account_type: str | None) -> AccountType:
    if document_type == DocumentType.CREDIT_CARD_STATEMENT:
        return AccountType.CREDIT_CARD
    if account_type in {member.value for member in AccountType}:
        return AccountType(account_type)
    return AccountType.SAVINGS


async def resolve(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    bank_code: str | None,
    bank_name: str | None,
    document_type: DocumentType,
    account_last4: str | None,
    account_type: str | None = None,
) -> uuid.UUID:
    """Find the account this statement belongs to, creating it if new."""
    code = (bank_code or "UNKNOWN").upper()
    kind = account_type_for(document_type, account_type)
    last4 = (account_last4 or UNKNOWN_LAST4)[-4:]
    fingerprint = account_fingerprint(tenant_id, code, str(kind), last4)

    existing = (
        await session.execute(
            text("SELECT id FROM accounts WHERE account_fingerprint = :fingerprint"),
            {"fingerprint": fingerprint},
        )
    ).one_or_none()
    if existing is not None:
        return existing.id

    account_id = uuid.uuid4()
    await session.execute(
        text(
            """
            INSERT INTO accounts (
                id, tenant_id, bank_code, bank_name, account_type, status,
                account_last4, account_fingerprint, display_name
            ) VALUES (
                :id, :tenant_id, :bank_code, :bank_name, :account_type, 'active',
                :last4, :fingerprint, :display_name
            )
            ON CONFLICT (tenant_id, account_fingerprint) DO NOTHING
            """
        ),
        {
            "id": account_id,
            "tenant_id": tenant_id,
            "bank_code": code,
            "bank_name": bank_name,
            "account_type": str(kind),
            "last4": last4,
            "fingerprint": fingerprint,
            "display_name": f"{bank_name or code} ••••{last4}",
        },
    )

    # A concurrent import of a second statement for the same account can win the
    # insert. Re-read rather than assume the id we generated is the one stored.
    row = (
        await session.execute(
            text("SELECT id FROM accounts WHERE account_fingerprint = :fingerprint"),
            {"fingerprint": fingerprint},
        )
    ).one()
    return row.id


async def record_statement_coverage(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    period_start: date | None,
    period_end: date | None,
    closing_balance: Decimal | None,
    credit_limit: Decimal | None = None,
) -> None:
    """Widen the account's known coverage and refresh its last-seen balance.

    The balance is *extracted*, never computed from the ledger: it is only as
    current as the most recent import, and presenting a derived figure as a
    bank balance would be inventing a number the bank never printed.
    """
    await session.execute(
        text(
            """
            -- Every parameter is CAST explicitly. A bind that appears only in
            -- `IS NULL` and a CASE branch gives asyncpg nothing to infer a type
            -- from, and it refuses the statement with AmbiguousParameterError
            -- rather than guessing.
            UPDATE accounts
            SET coverage_start = LEAST(
                    COALESCE(coverage_start, CAST(:start AS date)), CAST(:start AS date)
                ),
                coverage_end = GREATEST(
                    COALESCE(coverage_end, CAST(:end AS date)), CAST(:end AS date)
                ),
                current_balance = CASE
                    WHEN CAST(:closing AS numeric) IS NULL THEN current_balance
                    WHEN balance_as_of IS NULL OR CAST(:end AS date) >= balance_as_of
                        THEN CAST(:closing AS numeric)
                    ELSE current_balance
                END,
                balance_as_of = CASE
                    WHEN CAST(:closing AS numeric) IS NULL THEN balance_as_of
                    WHEN balance_as_of IS NULL OR CAST(:end AS date) >= balance_as_of
                        THEN CAST(:end AS date)
                    ELSE balance_as_of
                END,
                credit_limit = COALESCE(CAST(:credit_limit AS numeric), credit_limit),
                last_imported_at = now()
            WHERE id = :id
            """
        ),
        {
            "id": account_id,
            "start": period_start,
            "end": period_end,
            "closing": closing_balance,
            "credit_limit": credit_limit,
        },
    )


async def list_accounts(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                """
                SELECT a.id, a.bank_code, a.bank_name, a.account_type, a.status,
                       a.account_last4, a.display_name, a.current_balance,
                       a.balance_as_of, a.credit_limit, a.coverage_start,
                       a.coverage_end, a.last_imported_at, a.created_at,
                       -- DISTINCT on both. Joining transactions *and*
                       -- statements multiplies the rows, so a plain COUNT
                       -- reports 54 transactions as 108 once a second
                       -- statement exists for the account.
                       COUNT(DISTINCT t.id) AS transaction_count,
                       COUNT(DISTINCT s.id) AS statement_count
                FROM accounts a
                LEFT JOIN transactions t ON t.account_id = a.id
                LEFT JOIN statements s ON s.account_id = a.id AND s.deleted_at IS NULL
                WHERE a.deleted_at IS NULL
                GROUP BY a.id
                ORDER BY a.bank_code, a.account_last4
                """
            )
        )
    ).all()
    return [dict(row._mapping) for row in rows]
