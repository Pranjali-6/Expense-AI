"""What the Privacy Center shows.

Every number here is measured, not asserted. The allow-list is rendered from
``AIPayload``'s own fields, so the screen cannot claim a narrower perimeter than
the code enforces; the counters come from the gateway; the incident list comes
from rows the gateway wrote when it refused to send something.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.router import implemented_providers, known_providers
from app.core.config import settings
from app.privacy.allowlist import ALLOWED_FIELDS, AIPayload

#: Categories of data that never leave, stated explicitly. Rendered as a list
#: beside the allow-list so the screen answers "what is *not* sent?" — which is
#: the question a person actually has.
NEVER_SENT: tuple[str, ...] = (
    "The PDF itself, or any part of it",
    "Account numbers and card numbers",
    "Exact amounts and balances",
    "Transaction descriptions from the statement",
    "Names of people, including payees on transfers",
    "UPI IDs, IFSC codes, PAN, Aadhaar, GSTIN",
    "Email addresses, phone numbers and postal addresses",
    "Statement numbers and reference numbers",
    "Anything at all when AI is switched off",
)


def allow_list() -> list[dict[str, Any]]:
    """The permitted fields, read from the model rather than hardcoded."""
    descriptions = {
        "merchant": "A business name — only when verified in the merchant "
                    "dictionary, or proven a merchant by the payment rail",
        "amount_bucket": "A coarse range such as ₹500–1,000. Never the exact amount",
        "direction": "Whether money went out or came in",
        "payment_method": "The rail: UPI, card, NEFT and so on",
        "mcc_hint": "A four-digit industry code from the dictionary",
        "day_of_week": "The weekday only, never the date",
    }
    fields = AIPayload.model_fields
    return [
        {
            "name": name,
            "description": descriptions.get(name, ""),
            "optional": not fields[name].is_required() if name in fields else True,
        }
        for name in ALLOWED_FIELDS
    ]


async def summary(session: AsyncSession) -> dict[str, Any]:
    counters = (
        await session.execute(
            text(
                """
                SELECT
                    COALESCE(SUM(ai_calls_made), 0) AS ai_calls_made,
                    COALESCE(SUM(payloads_blocked), 0) AS payloads_blocked,
                    COALESCE(SUM(injections_quarantined), 0) AS injections_quarantined,
                    COALESCE(SUM(outputs_rejected), 0) AS outputs_rejected,
                    COALESCE(SUM(input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(cost_inr), 0) AS cost_inr
                FROM privacy_counters
                """
            )
        )
    ).one()

    this_month = (
        await session.execute(
            text(
                "SELECT COALESCE(SUM(cost_inr), 0) AS spent FROM privacy_counters "
                "WHERE day >= date_trunc('month', CURRENT_DATE)"
            )
        )
    ).one()

    by_source = (
        await session.execute(
            text(
                """
                SELECT category_source, count(*) AS count
                FROM transactions
                GROUP BY category_source
                ORDER BY count DESC
                """
            )
        )
    ).all()

    return {
        "ai_enabled": settings.ai_usable,
        "ai_configured": bool(settings.GEMINI_API_KEY),
        "provider": settings.AI_PROVIDER,
        "model": settings.AI_MODEL_CATEGORIZE,
        "known_providers": known_providers(),
        "implemented_providers": implemented_providers(),
        "allow_list": allow_list(),
        "never_sent": list(NEVER_SENT),
        "counters": {
            "ai_calls_made": int(counters.ai_calls_made),
            "payloads_blocked": int(counters.payloads_blocked),
            "injections_quarantined": int(counters.injections_quarantined),
            "outputs_rejected": int(counters.outputs_rejected),
            "input_tokens": int(counters.input_tokens),
            "output_tokens": int(counters.output_tokens),
        },
        "spend": {
            "total_inr": str(Decimal(str(counters.cost_inr))),
            "this_month_inr": str(Decimal(str(this_month.spent))),
            "monthly_budget_inr": str(settings.AI_MONTHLY_BUDGET_INR),
        },
        "categorisation_by_source": [
            {"source": row.category_source, "count": int(row.count)} for row in by_source
        ],
    }


async def incidents(session: AsyncSession, *, limit: int = 50) -> list[dict[str, Any]]:
    """Recent times a control fired.

    Each row names the detector, never what it matched — storing the evidence
    would itself be the leak.
    """
    rows = (
        await session.execute(
            text(
                """
                SELECT id, kind, detector, field_name, provider, model_name,
                       context, created_at
                FROM privacy_incidents
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"limit": limit},
        )
    ).all()
    return [dict(row._mapping) for row in rows]
