"""Reading and correcting the ledger.

Two rules shape everything here.

**Originals are never touched.** A correction writes ``corrected_*``; the
effective value is a PostgreSQL generated column computing
``COALESCE(corrected, original)``. There is no code path that can set the
effective value directly, because there is no such column to set. What the bank
printed stays recoverable forever, which is what makes a disputed transaction
arguable years later.

**A human correction outranks everything.** Setting ``is_verified`` puts the row
behind a database trigger that rejects any AI-sourced write to it. The
protection is not a service-layer convention that a future code path could
forget — it is enforced by the row itself.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, ValidationFailedError
from app.models.enums import CategorySource, Direction, ReviewStatus
from app.observability import metrics

#: Columns a user may correct. Anything absent is either derived, frozen, or
#: belongs to the system — an allow-list rather than a deny-list, so a new
#: column is not editable by accident.
CORRECTABLE = {
    "txn_date": "corrected_txn_date",
    "description": "corrected_description",
    "amount": "corrected_amount",
    "direction": "corrected_direction",
    "merchant": "corrected_merchant",
    "payment_method": "corrected_payment_method",
}

_SELECT = """
    SELECT t.id, t.account_id, t.statement_id,
           t.txn_date, t.original_value_date AS value_date,
           t.description, t.amount, t.direction,
           t.merchant, t.payment_method, t.original_balance_after AS balance_after,
           t.original_description, t.original_amount, t.original_direction,
           t.original_merchant, t.original_txn_date,
           t.category_id, t.subcategory_id, t.category_source, t.category_reason,
           t.movement_type, t.is_expense, t.transfer_group_id,
           t.confidence_extraction, t.confidence_merchant,
           t.confidence_category, t.confidence_validation, t.confidence_min,
           t.field_confidence, t.review_status, t.is_verified, t.verified_at,
           t.source_page, t.source_row, t.created_at,
           c.slug AS category_slug, c.name AS category_name, c.color AS category_color,
           sc.slug AS subcategory_slug, sc.name AS subcategory_name,
           a.bank_code, a.bank_name, a.account_last4, a.account_type,
           s.trust_status AS statement_trust_status
    FROM transactions t
    LEFT JOIN categories c ON c.id = t.category_id
    LEFT JOIN subcategories sc ON sc.id = t.subcategory_id
    LEFT JOIN accounts a ON a.id = t.account_id
    LEFT JOIN statements s ON s.id = t.statement_id
"""


def _filter_clauses(
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    category_slug: str | None = None,
    account_id: uuid.UUID | None = None,
    merchant: str | None = None,
    merchant_like: str | None = None,
    search: str | None = None,
    direction: str | None = None,
    review_status: str | None = None,
    is_expense: bool | None = None,
    min_amount: Decimal | None = None,
    max_amount: Decimal | None = None,
    max_confidence: Decimal | None = None,
) -> tuple[str, dict[str, Any]]:
    """Build the WHERE clause shared by every read of the ledger.

    One implementation, so a filtered *count*, a filtered *page* and a filtered
    *sum* cannot disagree about what "food in March" means. The assistant reads
    through the same function the ledger screen does, which is what makes an
    answer and the "open in Transactions" link behind it show the same rows.

    Every filter is a bound parameter. None is interpolated, and the tenant is
    never one of them — RLS scopes the query from the session GUC, so a caller
    cannot widen it by supplying an id.
    """
    clauses: list[str] = []
    params: dict[str, Any] = {}

    if date_from:
        clauses.append("t.txn_date >= :date_from")
        params["date_from"] = date_from
    if date_to:
        clauses.append("t.txn_date <= :date_to")
        params["date_to"] = date_to
    if category_slug:
        clauses.append("c.slug = :category_slug")
        params["category_slug"] = category_slug
    if account_id:
        clauses.append("t.account_id = :account_id")
        params["account_id"] = account_id
    if merchant:
        clauses.append("t.merchant = :merchant")
        params["merchant"] = merchant
    if merchant_like:
        # Merchant only, never description: a description match would pull in
        # rows whose *counterparty name* happens to contain the search term.
        clauses.append("t.merchant ILIKE :merchant_like")
        params["merchant_like"] = f"%{merchant_like}%"
    if search:
        clauses.append("(t.description ILIKE :search OR t.merchant ILIKE :search)")
        params["search"] = f"%{search}%"
    if direction:
        clauses.append("t.direction = :direction")
        params["direction"] = direction
    if review_status:
        clauses.append("t.review_status = :review_status")
        params["review_status"] = review_status
    if is_expense is not None:
        clauses.append("t.is_expense = :is_expense")
        params["is_expense"] = is_expense
    if min_amount is not None:
        clauses.append("t.amount >= :min_amount")
        params["min_amount"] = min_amount
    if max_amount is not None:
        clauses.append("t.amount <= :max_amount")
        params["max_amount"] = max_amount
    if max_confidence is not None:
        clauses.append("t.confidence_min <= :max_confidence")
        params["max_confidence"] = max_confidence

    return (f"WHERE {' AND '.join(clauses)}" if clauses else ""), params


#: The filter names ``_filter_clauses`` understands. Kept as data so the public
#: readers can declare them explicitly — a ``**kwargs`` passthrough would make a
#: misspelled filter a silently unfiltered query, which on a ledger means
#: showing someone more rows than they asked for and calling it an answer.
FILTER_FIELDS: tuple[str, ...] = (
    "date_from", "date_to", "category_slug", "account_id", "merchant",
    "merchant_like", "search", "direction", "review_status", "is_expense",
    "min_amount", "max_amount", "max_confidence",
)


async def list_transactions(
    session: AsyncSession,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    category_slug: str | None = None,
    account_id: uuid.UUID | None = None,
    merchant: str | None = None,
    merchant_like: str | None = None,
    search: str | None = None,
    direction: str | None = None,
    review_status: str | None = None,
    is_expense: bool | None = None,
    min_amount: Decimal | None = None,
    max_amount: Decimal | None = None,
    max_confidence: Decimal | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Filtered ledger page plus the total matching count."""
    where, params = _filter_clauses(
        date_from=date_from, date_to=date_to, category_slug=category_slug,
        account_id=account_id, merchant=merchant, merchant_like=merchant_like,
        search=search, direction=direction, review_status=review_status,
        is_expense=is_expense, min_amount=min_amount, max_amount=max_amount,
        max_confidence=max_confidence,
    )

    total = (
        await session.execute(
            text(
                f"""
                SELECT count(*) FROM transactions t
                LEFT JOIN categories c ON c.id = t.category_id
                {where}
                """
            ),
            params,
        )
    ).scalar_one()

    rows = (
        await session.execute(
            text(
                f"""
                {_SELECT}
                {where}
                ORDER BY t.txn_date DESC, t.created_at DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            {**params, "limit": limit, "offset": offset},
        )
    ).all()

    return [dict(row._mapping) for row in rows], total


async def sum_transactions(
    session: AsyncSession,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    category_slug: str | None = None,
    account_id: uuid.UUID | None = None,
    merchant: str | None = None,
    merchant_like: str | None = None,
    search: str | None = None,
    direction: str | None = None,
    review_status: str | None = None,
    is_expense: bool | None = None,
    min_amount: Decimal | None = None,
    max_amount: Decimal | None = None,
    max_confidence: Decimal | None = None,
) -> dict[str, Any]:
    """Count and total for the same filters, without paging through the rows.

    The assistant needs "how much", not "which ones", and paging fifty rows at
    a time to add them up in Python would be both slow and a second place for
    the arithmetic to live.
    """
    where, params = _filter_clauses(
        date_from=date_from, date_to=date_to, category_slug=category_slug,
        account_id=account_id, merchant=merchant, merchant_like=merchant_like,
        search=search, direction=direction, review_status=review_status,
        is_expense=is_expense, min_amount=min_amount, max_amount=max_amount,
        max_confidence=max_confidence,
    )
    row = (
        await session.execute(
            text(
                f"""
                SELECT COUNT(*) AS matched,
                       COALESCE(SUM(t.amount), 0) AS total,
                       MIN(t.txn_date) AS first_date,
                       MAX(t.txn_date) AS last_date
                FROM transactions t
                LEFT JOIN categories c ON c.id = t.category_id
                {where}
                """
            ),
            params,
        )
    ).one()
    return {
        "matched": int(row.matched),
        "total": Decimal(str(row.total or 0)).quantize(Decimal("0.01")),
        "first_date": row.first_date,
        "last_date": row.last_date,
    }


async def get_transaction(session: AsyncSession, transaction_id: uuid.UUID) -> dict[str, Any]:
    row = (
        await session.execute(text(f"{_SELECT} WHERE t.id = :id"), {"id": transaction_id})
    ).one_or_none()
    if row is None:
        # 404 rather than 403 for another tenant's row: distinguishing them
        # would confirm the id exists.
        raise NotFoundError("Transaction not found.")
    return dict(row._mapping)


async def correct(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    transaction_id: uuid.UUID,
    changes: dict[str, Any],
    category_slug: str | None = None,
    subcategory_slug: str | None = None,
    verify: bool = True,
) -> dict[str, Any]:
    """Apply a user's correction, preserving the original.

    Every field written here is a ``corrected_*`` column. The audit row records
    the before and after so the sequence of corrections survives too — the
    frozen columns preserve the truth, the audit preserves the story.
    """
    current = await get_transaction(session, transaction_id)

    updates: dict[str, Any] = {}
    audit_rows: list[tuple[str, Any, Any]] = []

    for field, column in CORRECTABLE.items():
        if field not in changes:
            continue
        value = changes[field]
        if field == "direction" and value not in {str(d) for d in Direction}:
            raise ValidationFailedError(f"{value!r} is not a valid direction.")
        if field == "amount" and value is not None and Decimal(value) < 0:
            raise ValidationFailedError("Amount cannot be negative; use direction instead.")
        if value == current.get(field):
            continue
        updates[column] = value
        audit_rows.append((field, current.get(field), value))

    if category_slug is not None:
        category = (
            await session.execute(
                text("SELECT id FROM categories WHERE slug = :slug"), {"slug": category_slug}
            )
        ).one_or_none()
        if category is None:
            raise ValidationFailedError(f"Unknown category {category_slug!r}.")

        # Labelled by what the system had said *before* the human disagreed.
        # That is the accuracy signal worth watching: corrections against
        # `ai` mean the model is wrong often, corrections against
        # `deterministic_rule` mean a rule needs changing, and the two call for
        # completely different responses.
        if category_slug != current.get("category_slug"):
            metrics.user_corrections_total.labels(
                previous_source=str(current.get("category_source") or "none")
            ).inc()

        subcategory_id = None
        if subcategory_slug:
            sub = (
                await session.execute(
                    text(
                        "SELECT id FROM subcategories "
                        "WHERE category_id = :category_id AND slug = :slug"
                    ),
                    {"category_id": category.id, "slug": subcategory_slug},
                )
            ).one_or_none()
            if sub is None:
                raise ValidationFailedError(f"Unknown subcategory {subcategory_slug!r}.")
            subcategory_id = sub.id

        if category.id != current.get("category_id"):
            audit_rows.append(
                ("category", current.get("category_slug"), category_slug)
            )
        updates["category_id"] = category.id
        updates["subcategory_id"] = subcategory_id
        updates["category_source"] = str(CategorySource.USER_RULE)
        updates["category_reason"] = None

        # Learn from it. One correction becomes a standing rule at the top of
        # the cascade, so the same merchant is never sent to a model again.
        from app.services import categorization

        await categorization.learn_from_correction(
            session,
            tenant_id=tenant_id,
            user_id=user_id,
            merchant=changes.get("merchant") or current.get("merchant"),
            category_id=category.id,
            subcategory_id=subcategory_id,
        )

    if not updates:
        return current

    assignments = ", ".join(f"{column} = :{column}" for column in updates)
    verification = (
        ", is_verified = true, verified_by = :user_id, verified_at = now(), "
        f"review_status = '{ReviewStatus.RESOLVED.value}'"
        if verify else ""
    )

    await session.execute(
        text(
            f"""
            UPDATE transactions
            SET {assignments}{verification}
            WHERE id = :id
            """
        ),
        {**updates, "id": transaction_id, "user_id": user_id},
    )

    for field, old, new in audit_rows:
        await session.execute(
            text(
                """
                INSERT INTO transaction_audit (
                    tenant_id, transaction_id, changed_by, actor_kind,
                    field_name, old_value, new_value, reason, changed_at
                ) VALUES (
                    :tenant_id, :transaction_id, :user_id, 'user',
                    :field, :old_value, :new_value, 'user_correction', now()
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "transaction_id": transaction_id,
                "user_id": user_id,
                "field": field,
                "old_value": None if old is None else str(old),
                "new_value": None if new is None else str(new),
            },
        )

    return await get_transaction(session, transaction_id)


async def approve(
    session: AsyncSession, *, user_id: uuid.UUID, transaction_ids: list[uuid.UUID]
) -> int:
    """Accept rows as read, without changing a value.

    Marks them verified: the reviewer looked and agreed, which is a stronger
    statement than "the model was confident" and protects the row from later
    AI-sourced writes.
    """
    if not transaction_ids:
        return 0
    result = await session.execute(
        text(
            """
            UPDATE transactions
            SET review_status = :status,
                is_verified = true,
                verified_by = :user_id,
                verified_at = now()
            WHERE id = ANY(:ids)
            RETURNING id
            """
        ),
        {"status": str(ReviewStatus.RESOLVED), "user_id": user_id, "ids": transaction_ids},
    )
    return len(result.all())


async def explain(session: AsyncSession, transaction_id: uuid.UUID) -> dict[str, Any]:
    """Why was this transaction categorised this way?

    Read from what was stored at decision time rather than re-derived. A
    recomputed explanation can disagree with the decision it claims to explain —
    the rules may have changed since — and an explanation that does not match
    the data is worse than none.
    """
    row = await get_transaction(session, transaction_id)
    reason = row.get("category_reason") or {}
    source = row.get("category_source")

    sentence = _explanation_sentence(source, reason, row)

    return {
        "transaction_id": str(transaction_id),
        "category_slug": row.get("category_slug"),
        "category_name": row.get("category_name"),
        "source": source,
        "sentence": sentence,
        "reason": reason,
        "confidence": {
            "extraction": row["confidence_extraction"],
            "merchant": row["confidence_merchant"],
            "category": row["confidence_category"],
            "validation": row["confidence_validation"],
            "minimum": row["confidence_min"],
            "weakest": reason.get("weakest_dimension"),
        },
        "provenance": {
            "statement_id": str(row["statement_id"]) if row["statement_id"] else None,
            "page": row["source_page"],
            "row": row["source_row"],
            "statement_trust_status": row["statement_trust_status"],
        },
    }


def _explanation_sentence(source: str | None, reason: dict, row: dict) -> str:
    merchant = row.get("merchant") or "this transaction"
    category = row.get("category_name") or "Other"

    if source == CategorySource.USER_RULE:
        return f"You set this. {merchant} → {category}."
    if source == CategorySource.VERIFIED_MERCHANT_RULE:
        alias = reason.get("alias")
        via = f' — the statement said "{alias}"' if alias else ""
        return (
            f"Recognised {merchant} in the merchant dictionary{via}, "
            f"which is categorised as {category}."
        )
    if source == CategorySource.DETERMINISTIC_RULE:
        matched = reason.get("matched")
        quoted = f' on "{matched}"' if matched else ""
        return (
            f"A rule matched{quoted}, which identifies this as {category}. "
            "No AI was involved."
        )
    if source == CategorySource.HISTORICAL_PATTERN:
        count = reason.get("sample_count")
        return f"You have categorised {merchant} as {category} {count} times before."
    if source == CategorySource.AI_MODEL:
        model = reason.get("model", "the model")
        score = reason.get("score")
        return (
            f"No rule matched, so {model} suggested {category}"
            f"{f' (confidence {score})' if score else ''}. "
            "It saw only the merchant name and an amount range."
        )
    return (
        "Nothing recognised this transaction, so it is uncategorised. "
        "Setting a category here teaches the system for next time."
    )


async def audit_trail(session: AsyncSession, transaction_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                """
                SELECT a.field_name, a.old_value, a.new_value, a.actor_kind,
                       a.reason, a.changed_at, u.full_name AS changed_by_name
                FROM transaction_audit a
                LEFT JOIN users u ON u.id = a.changed_by
                WHERE a.transaction_id = :id
                ORDER BY a.changed_at DESC
                """
            ),
            {"id": transaction_id},
        )
    ).all()
    return [dict(row._mapping) for row in rows]


async def review_stats(session: AsyncSession) -> dict[str, Any]:
    row = (
        await session.execute(
            text(
                """
                SELECT
                    count(*) FILTER (WHERE review_status = 'review_required') AS review_required,
                    count(*) FILTER (WHERE review_status = 'flagged') AS flagged,
                    count(*) FILTER (WHERE review_status = 'auto_approved') AS auto_approved,
                    count(*) FILTER (WHERE review_status = 'resolved') AS resolved,
                    count(*) AS total,
                    count(*) FILTER (WHERE category_id IS NULL) AS uncategorised
                FROM transactions
                """
            )
        )
    ).one()
    return dict(row._mapping)


async def apply_to_similar(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    transaction_id: uuid.UUID,
    category_slug: str,
) -> int:
    """Apply a correction to every other transaction with the same merchant.

    Only rows that are **not already verified** are touched. A previous human
    decision is never overwritten by a later bulk action — including one made by
    the same person, who may not remember deciding differently.
    """
    source = await get_transaction(session, transaction_id)
    merchant = source.get("merchant")
    if not merchant:
        raise ValidationFailedError("This transaction has no merchant to match on.")

    category = (
        await session.execute(
            text("SELECT id FROM categories WHERE slug = :slug"), {"slug": category_slug}
        )
    ).one_or_none()
    if category is None:
        raise ValidationFailedError(f"Unknown category {category_slug!r}.")

    result = await session.execute(
        text(
            """
            UPDATE transactions
            SET category_id = :category_id,
                subcategory_id = NULL,
                category_source = :source,
                category_reason = CAST(:reason AS jsonb)
            WHERE merchant = :merchant
              AND id <> :id
              AND is_verified = false
            RETURNING id
            """
        ),
        {
            "category_id": category.id,
            "source": str(CategorySource.USER_RULE),
            "reason": '{"tier": "user_rule", "applied_to_similar": true}',
            "merchant": merchant,
            "id": transaction_id,
        },
    )
    return len(result.all())
