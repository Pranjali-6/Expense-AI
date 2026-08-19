"""Categories, and the rules that outrank everything else.

A user rule sits at the top of the categorisation cascade: above the merchant
dictionary, above the deterministic rules, above any model. That is the whole
point of it — someone who has told the system that Blinkit is Grocery should
never be told otherwise by a language model on the next import.

So the endpoints here are small but consequential. Creating a rule changes how
future imports are read; deleting one changes it back. Both are audited, and
neither touches transactions that already exist — a rule is a decision about
what happens next, and rewriting settled history from it would silently move
figures a user has already seen and reconciled. ``POST /rules/{id}/apply``
exists for the case where they *do* want the history changed, and says so.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from app.core.deps import CurrentUser, TenantSession, parse_uuid
from app.core.errors import NotFoundError, ValidationFailedError
from app.models.enums import AuditAction
from app.services import audit

router = APIRouter(prefix="/categories", tags=["categories"])


class RuleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    merchant_pattern: str = Field(min_length=1, max_length=255)
    category_slug: str = Field(min_length=1, max_length=64)
    subcategory_slug: str | None = Field(default=None, max_length=64)
    match_type: str = Field(default="exact", pattern="^(exact|contains)$")
    min_amount: Decimal | None = None
    max_amount: Decimal | None = None


@router.get("", summary="Every category and subcategory")
async def list_categories(session: TenantSession) -> list[dict[str, Any]]:
    """The fixed taxonomy, with this tenant's usage counts.

    Counts come from the caller's own ledger through Row Level Security, so the
    same global category list reads differently for each tenant — which is the
    useful version: "you have 214 transactions in Food" is worth showing, "there
    are 22 categories" is not.
    """
    rows = (
        await session.execute(
            text(
                """
                SELECT c.id, c.slug, c.name, c.color, c.icon, c.is_expense, c.is_income,
                       c.sort_order,
                       COUNT(t.id) AS transaction_count,
                       COALESCE(SUM(t.amount) FILTER (WHERE t.is_expense), 0) AS total
                FROM categories c
                LEFT JOIN transactions t ON t.category_id = c.id
                GROUP BY c.id, c.slug, c.name, c.color, c.icon,
                         c.is_expense, c.is_income, c.sort_order
                ORDER BY c.sort_order, c.name
                """
            )
        )
    ).all()

    subs = (
        await session.execute(
            text(
                "SELECT id, category_id, slug, name FROM subcategories ORDER BY name"
            )
        )
    ).all()
    by_category: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for sub in subs:
        by_category.setdefault(sub.category_id, []).append(
            {"slug": sub.slug, "name": sub.name}
        )

    return [
        {
            "slug": row.slug,
            "name": row.name,
            "color": row.color,
            "icon": row.icon,
            "is_expense": row.is_expense,
            "is_income": row.is_income,
            "transaction_count": int(row.transaction_count),
            "total": str(row.total),
            "subcategories": by_category.get(row.id, []),
        }
        for row in rows
    ]


@router.get("/rules", summary="Your categorisation rules")
async def list_rules(session: TenantSession) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                """
                SELECT r.id, r.merchant_pattern, r.match_type, r.min_amount,
                       r.max_amount, r.is_active, r.times_applied,
                       r.last_applied_at, r.created_at,
                       c.slug AS category_slug, c.name AS category_name, c.color,
                       sc.slug AS subcategory_slug, sc.name AS subcategory_name
                FROM user_category_rules r
                JOIN categories c ON c.id = r.category_id
                LEFT JOIN subcategories sc ON sc.id = r.subcategory_id
                ORDER BY r.created_at DESC
                """
            )
        )
    ).all()
    return [
        {
            "id": str(row.id),
            "merchant_pattern": row.merchant_pattern,
            "match_type": row.match_type,
            "min_amount": str(row.min_amount) if row.min_amount is not None else None,
            "max_amount": str(row.max_amount) if row.max_amount is not None else None,
            "is_active": row.is_active,
            "times_applied": row.times_applied,
            "last_applied_at": (
                row.last_applied_at.isoformat() if row.last_applied_at else None
            ),
            "created_at": row.created_at.isoformat(),
            "category_slug": row.category_slug,
            "category_name": row.category_name,
            "color": row.color,
            "subcategory_slug": row.subcategory_slug,
            "subcategory_name": row.subcategory_name,
        }
        for row in rows
    ]


async def _resolve(session, category_slug: str, subcategory_slug: str | None):
    category = (
        await session.execute(
            text("SELECT id FROM categories WHERE slug = :slug"), {"slug": category_slug}
        )
    ).one_or_none()
    if category is None:
        raise ValidationFailedError(f"Unknown category {category_slug!r}.")

    subcategory_id = None
    if subcategory_slug:
        sub = (
            await session.execute(
                text(
                    "SELECT id FROM subcategories WHERE slug = :slug AND category_id = :cid"
                ),
                {"slug": subcategory_slug, "cid": category.id},
            )
        ).one_or_none()
        if sub is None:
            raise ValidationFailedError(
                f"{subcategory_slug!r} is not a subcategory of {category_slug!r}."
            )
        subcategory_id = sub.id
    return category.id, subcategory_id


@router.post("/rules", summary="Create a rule that outranks everything")
async def create_rule(
    payload: RuleRequest, session: TenantSession, current_user: CurrentUser
) -> dict[str, Any]:
    if (
        payload.min_amount is not None
        and payload.max_amount is not None
        and payload.min_amount > payload.max_amount
    ):
        raise ValidationFailedError("The minimum amount is above the maximum.")

    category_id, subcategory_id = await _resolve(
        session, payload.category_slug, payload.subcategory_slug
    )

    row = (
        await session.execute(
            text(
                """
                INSERT INTO user_category_rules (
                    tenant_id, created_by, merchant_pattern, match_type,
                    category_id, subcategory_id, min_amount, max_amount
                ) VALUES (
                    :tenant_id, :user_id, :pattern, :match_type,
                    :category_id, :subcategory_id, :min_amount, :max_amount
                )
                ON CONFLICT (tenant_id, merchant_pattern, match_type, account_id)
                DO UPDATE SET category_id = EXCLUDED.category_id,
                              subcategory_id = EXCLUDED.subcategory_id,
                              min_amount = EXCLUDED.min_amount,
                              max_amount = EXCLUDED.max_amount,
                              is_active = true
                RETURNING id
                """
            ),
            {
                "tenant_id": current_user.tenant_id,
                "user_id": current_user.id,
                "pattern": payload.merchant_pattern.strip(),
                "match_type": payload.match_type,
                "category_id": category_id,
                "subcategory_id": subcategory_id,
                "min_amount": payload.min_amount,
                "max_amount": payload.max_amount,
            },
        )
    ).one()

    await audit.record(
        session,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        action=AuditAction.RULE_CREATE,
        resource_type="user_category_rule",
        resource_id=row.id,
        # The category is a fixed slug from a closed set; the merchant pattern
        # is the user's own text and stays out of the audit row.
        details={"category": payload.category_slug, "match_type": payload.match_type},
    )
    return {"id": str(row.id)}


@router.delete("/rules/{rule_id}", summary="Delete a rule")
async def delete_rule(
    rule_id: str, session: TenantSession, current_user: CurrentUser
) -> dict[str, bool]:
    identifier = parse_uuid(rule_id, "rule_id")
    result = await session.execute(
        text("DELETE FROM user_category_rules WHERE id = :id"), {"id": identifier}
    )
    if not result.rowcount:
        raise NotFoundError("That rule does not exist.")

    await audit.record(
        session,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        action=AuditAction.RULE_DELETE,
        resource_type="user_category_rule",
        resource_id=identifier,
    )
    return {"deleted": True}
