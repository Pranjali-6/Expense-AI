"""The categorisation cascade.

    User Rule → Verified Merchant Rule → Deterministic Rule
              → Historical User Pattern → AI → Other

Tiers one to three are already decided by the time a transaction reaches the
ledger: the parser's ``enrich`` applies the merchant dictionary and the
deterministic narration rules. This module adds the tiers that need the
database — the user's own rules, their history — and, only when everything
deterministic has failed, asks a model.

**AI is the narrowest tier on purpose.** It runs on rows where nothing else
matched *and* the privacy gateway is willing to send something, which after the
rail rules means: an unrecognised business on a card or an explicit
person-to-merchant UPI payment. That is a genuinely useful set — it is every
shop the seeded dictionary has never heard of — and it excludes the case where a
counterparty is a person.

**A user's decision is permanent.** Correcting a category writes a rule that
outranks every automatic tier from then on, and a database trigger rejects any
AI-sourced write to a verified row. The model cannot overrule a person, not by
policy but by construction.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.models.enums import CategorySource
from app.privacy import gateway
from app.services import confidence as confidence_service

logger = get_logger(__name__)

#: How many times a user must have confirmed merchant → category before the
#: pattern is trusted as a tier. Below this it is a coincidence, and applying it
#: would teach the system from a single accident.
HISTORY_THRESHOLD = 3

#: Confidence assigned when a tier fires. The AI tier's is the model's own
#: self-report, capped — a model's confidence in itself is evidence, not proof.
_AI_CONFIDENCE_CAP = Decimal("0.92")


@dataclass(slots=True)
class CascadeOutcome:
    considered: int = 0
    by_user_rule: int = 0
    by_history: int = 0
    by_ai: int = 0
    left_uncategorised: int = 0
    ai_calls: int = 0
    ai_blocked: int = 0
    ai_quarantined: int = 0
    cost_inr: Decimal = Decimal("0")


async def _pending_rows(
    session: AsyncSession, statement_id: uuid.UUID
) -> list[dict[str, Any]]:
    """Rows the deterministic tiers could not settle.

    Verified rows are excluded outright: a human has decided, and nothing
    downstream gets to revisit that.
    """
    rows = (
        await session.execute(
            text(
                """
                SELECT t.id, t.merchant, t.description, t.amount, t.direction,
                       t.payment_method, t.txn_date, t.category_id,
                       t.category_source, t.confidence_merchant,
                       t.raw_row ->> 'merchant_slug' AS merchant_slug
                FROM transactions t
                WHERE t.statement_id = :statement_id
                  AND t.is_verified = false
                  AND (t.category_id IS NULL OR t.category_source = :fallback)
                """
            ),
            {"statement_id": statement_id, "fallback": str(CategorySource.FALLBACK_OTHER)},
        )
    ).all()
    return [dict(row._mapping) for row in rows]


async def _apply_user_rule(
    session: AsyncSession, merchant: str | None, amount: Decimal
) -> dict[str, Any] | None:
    """The top tier: a rule the user created by correcting something."""
    if not merchant:
        return None
    row = (
        await session.execute(
            text(
                """
                SELECT r.id, r.category_id, r.subcategory_id, c.slug AS category_slug,
                       r.merchant_pattern, r.created_at
                FROM user_category_rules r
                JOIN categories c ON c.id = r.category_id
                WHERE r.is_active = true
                  AND (
                      (r.match_type = 'exact' AND lower(r.merchant_pattern) = lower(:merchant))
                      OR (r.match_type = 'contains'
                          AND lower(:merchant) LIKE '%' || lower(r.merchant_pattern) || '%')
                  )
                  AND (r.min_amount IS NULL OR :amount >= r.min_amount)
                  AND (r.max_amount IS NULL OR :amount <= r.max_amount)
                ORDER BY r.priority, r.created_at DESC
                LIMIT 1
                """
            ),
            {"merchant": merchant, "amount": amount},
        )
    ).one_or_none()
    return dict(row._mapping) if row else None


async def _historical_pattern(
    session: AsyncSession, merchant: str | None
) -> dict[str, Any] | None:
    """What this user has repeatedly confirmed for this merchant."""
    if not merchant:
        return None
    row = (
        await session.execute(
            text(
                """
                SELECT t.category_id, c.slug AS category_slug, count(*) AS sample_count
                FROM transactions t
                JOIN categories c ON c.id = t.category_id
                WHERE t.merchant = :merchant
                  AND t.is_verified = true
                  AND t.category_id IS NOT NULL
                GROUP BY t.category_id, c.slug
                HAVING count(*) >= :threshold
                ORDER BY count(*) DESC
                LIMIT 1
                """
            ),
            {"merchant": merchant, "threshold": HISTORY_THRESHOLD},
        )
    ).one_or_none()
    return dict(row._mapping) if row else None


async def _write_category(
    session: AsyncSession,
    *,
    transaction_id: uuid.UUID,
    category_id: uuid.UUID,
    subcategory_id: uuid.UUID | None,
    source: CategorySource,
    reason: dict[str, Any],
    category_confidence: Decimal,
) -> None:
    """Set the category and rescore the row.

    ``review_status`` is recomputed from the four dimensions rather than
    assumed: a row that reaches a confident category may still be held for
    review because its extraction or its statement is doubtful.
    """
    await session.execute(
        text(
            """
            UPDATE transactions
            SET category_id = :category_id,
                subcategory_id = :subcategory_id,
                category_source = :source,
                category_reason = CAST(:reason AS jsonb),
                confidence_category = :category_confidence,
                review_status = CASE
                    WHEN LEAST(confidence_extraction, confidence_merchant,
                               :category_confidence, confidence_validation) >= :approve
                        THEN 'auto_approved'
                    WHEN LEAST(confidence_extraction, confidence_merchant,
                               :category_confidence, confidence_validation) >= :flag
                        THEN 'flagged'
                    ELSE 'review_required'
                END
            WHERE id = :id
              AND is_verified = false
            """
        ),
        {
            "id": transaction_id,
            "category_id": category_id,
            "subcategory_id": subcategory_id,
            "source": str(source),
            "reason": json.dumps(reason),
            "category_confidence": category_confidence,
            "approve": confidence_service.AUTO_APPROVE_AT,
            "flag": confidence_service.FLAG_AT,
        },
    )


async def run_cascade(
    session: AsyncSession, *, tenant_id: uuid.UUID, statement_id: uuid.UUID
) -> CascadeOutcome:
    """Finish categorising a statement's transactions."""
    outcome = CascadeOutcome()
    rows = await _pending_rows(session, statement_id)
    outcome.considered = len(rows)

    for row in rows:
        transaction_id = row["id"]
        merchant = row["merchant"]

        # --- tier 1: the user's own rule -----------------------------------
        rule = await _apply_user_rule(session, merchant, row["amount"])
        if rule:
            await _write_category(
                session,
                transaction_id=transaction_id,
                category_id=rule["category_id"],
                subcategory_id=rule["subcategory_id"],
                source=CategorySource.USER_RULE,
                reason={
                    "tier": "user_rule",
                    "rule_id": str(rule["id"]),
                    "pattern": rule["merchant_pattern"],
                },
                category_confidence=Decimal("1.0000"),
            )
            outcome.by_user_rule += 1
            continue

        # --- tier 4: what this user has confirmed before --------------------
        pattern = await _historical_pattern(session, merchant)
        if pattern:
            await _write_category(
                session,
                transaction_id=transaction_id,
                category_id=pattern["category_id"],
                subcategory_id=None,
                source=CategorySource.HISTORICAL_PATTERN,
                reason={
                    "tier": "historical_pattern",
                    "sample_count": pattern["sample_count"],
                },
                category_confidence=Decimal("0.9000"),
            )
            outcome.by_history += 1
            continue

        # --- tier 5: ask a model, through the gateway -----------------------
        if not settings.ai_usable:
            outcome.left_uncategorised += 1
            continue

        result = await gateway.classify(
            session,
            tenant_id=tenant_id,
            transaction_id=transaction_id,
            merchant=merchant,
            merchant_is_known=bool(row["merchant_slug"]),
            description=row["description"] or "",
            amount=row["amount"],
            direction=row["direction"],
            payment_method=row["payment_method"],
            txn_date=row["txn_date"],
        )
        outcome.cost_inr += result.cost_inr

        if result.outcome == gateway.Outcome.QUARANTINED:
            outcome.ai_quarantined += 1
        elif result.outcome in {
            gateway.Outcome.PII_BLOCKED, gateway.Outcome.OUTPUT_REJECTED
        }:
            outcome.ai_blocked += 1

        if not result.ok:
            outcome.left_uncategorised += 1
            continue

        outcome.ai_calls += 1
        prediction = result.prediction
        assert prediction is not None

        category = (
            await session.execute(
                text("SELECT id FROM categories WHERE slug = :slug"),
                {"slug": prediction.category_slug},
            )
        ).one_or_none()
        if category is None:
            outcome.left_uncategorised += 1
            continue

        await _write_category(
            session,
            transaction_id=transaction_id,
            category_id=category.id,
            subcategory_id=None,
            source=CategorySource.AI_MODEL,
            reason={
                "tier": "ai_model",
                "model": result.model_name,
                "score": str(prediction.confidence),
                "fields_sent": result.fields_sent,
            },
            # The model's self-report, capped. A model's confidence in itself is
            # evidence, not proof, and an AI-sourced category should never reach
            # the auto-approve line on the model's own say-so.
            category_confidence=min(prediction.confidence, _AI_CONFIDENCE_CAP),
        )
        outcome.by_ai += 1

    logger.info(
        "cascade_completed",
        stage="categorize",
        statement_id=str(statement_id),
        count=outcome.considered,
        status="ok",
    )
    return outcome


async def learn_from_correction(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    merchant: str | None,
    category_id: uuid.UUID,
    subcategory_id: uuid.UUID | None = None,
) -> uuid.UUID | None:
    """Turn a correction into a standing rule.

    This is how the system learns. Correcting one Swiggy transaction to Food
    means every future Swiggy transaction is categorised by the top tier of the
    cascade — never by the model, and never overwritten by it.
    """
    if not merchant:
        return None

    rule_id = uuid.uuid4()
    row = (
        await session.execute(
            text(
                """
                INSERT INTO user_category_rules (
                    id, tenant_id, created_by, merchant_pattern, match_type,
                    category_id, subcategory_id, is_active, priority
                ) VALUES (
                    :id, :tenant_id, :user_id, :merchant, 'exact',
                    :category_id, :subcategory_id, true, 10
                )
                ON CONFLICT DO NOTHING
                RETURNING id
                """
            ),
            {
                "id": rule_id,
                "tenant_id": tenant_id,
                "user_id": user_id,
                "merchant": merchant,
                "category_id": category_id,
                "subcategory_id": subcategory_id,
            },
        )
    ).one_or_none()

    if row is None:
        # A rule for this merchant already exists; point it at the new category
        # rather than accumulating contradictory rules.
        await session.execute(
            text(
                """
                UPDATE user_category_rules
                SET category_id = :category_id,
                    subcategory_id = :subcategory_id,
                    created_by = :user_id,
                    is_active = true
                WHERE lower(merchant_pattern) = lower(:merchant)
                """
            ),
            {
                "category_id": category_id,
                "subcategory_id": subcategory_id,
                "user_id": user_id,
                "merchant": merchant,
            },
        )
        return None

    return rule_id
