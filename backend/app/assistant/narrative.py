"""The monthly report, in a sentence — written from the snapshot, never the ledger.

"From snapshots only" is the whole design, and it is a stronger statement than
it looks. The model is not handed a month of transactions and asked to
summarise; it is handed a `insight_snapshots` row that has already been
computed, rounded and stored, and asked to phrase it. Three things follow.

The prose and the cards below it cannot disagree, because they are the same
row. A dashboard that says ₹1,27,665 above a paragraph that says "just over
₹1.3 lakh" is a product nobody trusts twice, and the usual cause is two code
paths reading the same data at different times.

Nothing is recomputed at phrasing time, so the paragraph does not drift when a
statement is imported later. It describes the month as it was understood when
the snapshot was taken, and the snapshot is refreshed on its own schedule.

And the narrative is optional in the schema as well as in the product. The
column is nullable, the nightly job skips it with no key configured, and the
Insights screen renders the same snapshot as cards either way. A null narrative
is what "works with AI switched off" looks like in the database.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.base import AIProvider, Message
from app.ai.prompts import NARRATIVE_SYSTEM_INSTRUCTION
from app.ai.router import get_provider
from app.assistant import redaction, traceability
from app.assistant.period import month_label
from app.core.config import settings
from app.core.logging import get_logger
from app.models.enums import PrivacyIncidentKind
from app.privacy import gateway
from app.privacy.output_validator import validate_prose

logger = get_logger(__name__)


async def stored(session: AsyncSession, month: date) -> dict[str, Any] | None:
    """The narrative already written for this month, if any."""
    row = (
        await session.execute(
            text(
                """
                SELECT narrative, narrative_model, narrative_generated_at
                FROM insight_snapshots
                WHERE period_month = :month
                """
            ),
            {"month": month.replace(day=1)},
        )
    ).one_or_none()

    if row is None or not row.narrative:
        return None
    return {
        "text": row.narrative,
        "model_name": row.narrative_model,
        "generated_at": (
            row.narrative_generated_at.isoformat()
            if row.narrative_generated_at else None
        ),
    }


async def _snapshot_view(
    session: AsyncSession, month: date
) -> tuple[dict[str, Any], list[str]] | None:
    """The snapshot as the model may see it: rounded, redacted, finished."""
    row = (
        await session.execute(
            text(
                """
                SELECT s.period_month, s.total_expenses, s.total_income,
                       s.net_cash_flow, s.savings_rate, s.transaction_count,
                       s.unreviewed_count, s.untrusted_statement_count,
                       s.breakdown,
                       lc.name AS largest_category,
                       fg.name AS fastest_growing_category
                FROM insight_snapshots s
                LEFT JOIN categories lc ON lc.id = s.largest_category_id
                LEFT JOIN categories fg ON fg.id = s.fastest_growing_category_id
                WHERE s.period_month = :month
                """
            ),
            {"month": month.replace(day=1)},
        )
    ).one_or_none()

    if row is None:
        return None

    breakdown = row.breakdown or {}
    merchants = breakdown.get("top_merchants") or []
    names = [item.get("merchant") for item in merchants if item.get("merchant")]
    known = await redaction.business_merchants(session, names)

    withheld: list[str] = []
    top: list[dict[str, Any]] = []
    for item in merchants[:5]:
        name = item.get("merchant")
        safe = redaction.merchant_for_model(name, is_known=name in known)
        if name and safe is None:
            withheld.append(name)
        top.append(
            {
                "merchant": safe,
                "merchant_withheld": bool(name) and safe is None,
                "total_rupees": redaction.rupees(item.get("total")),
                "transaction_count": item.get("transaction_count"),
            }
        )

    recurring = breakdown.get("recurring_load") or {}

    view = {
        "month_label": month_label(row.period_month),
        "spending_rupees": redaction.rupees(row.total_expenses),
        "income_rupees": redaction.rupees(row.total_income),
        "net_cash_flow_rupees": redaction.rupees(row.net_cash_flow),
        "savings_rate_percent": redaction.percent(row.savings_rate),
        "transaction_count": int(row.transaction_count),
        "largest_category": row.largest_category,
        "fastest_growing_category": row.fastest_growing_category,
        "top_merchants": top,
        "recurring_charge_count": int(recurring.get("count") or 0),
        "recurring_monthly_rupees": redaction.rupees(
            recurring.get("monthly_equivalent")
        ),
        "figures_include_unverified": bool(
            row.unreviewed_count or row.untrusted_statement_count
        ),
        "awaiting_review_count": int(row.unreviewed_count),
        "from_untrusted_statements_count": int(row.untrusted_statement_count),
    }
    return view, withheld


async def generate(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    month: date,
    provider: AIProvider | None = None,
) -> str | None:
    """Write and store one month's narrative. Returns None if it was not written.

    None is an ordinary outcome, not an error: AI off, no snapshot, no key,
    budget spent, a rejected answer. Every one of them leaves the column null
    and the screen rendering cards, which is the state the product is designed
    around rather than degraded into.
    """
    if not settings.ai_usable:
        return None

    prepared = await _snapshot_view(session, month)
    if prepared is None:
        return None
    view, _withheld = prepared

    verified = redaction.verify(view)
    if not verified.ok:
        logger.error(
            "narrative_view_blocked", stage="narrative", error_code=verified.blocked_by
        )
        await gateway.record_usage(session, tenant_id=tenant_id, payloads_blocked=1)
        await gateway.record_incident(
            session,
            tenant_id=tenant_id,
            kind=PrivacyIncidentKind.PII_IN_PAYLOAD,
            detector=verified.blocked_by,
            context={"stage": "narrative"},
        )
        return None

    if not await gateway.within_budget(session):
        return None

    engine = provider or get_provider()
    if not engine.available():
        return None

    model = settings.AI_MODEL_ASSISTANT
    turn = await engine.converse(
        system_instruction=NARRATIVE_SYSTEM_INSTRUCTION,
        messages=[
            Message(
                role="user",
                text=(
                    "Here is the finished summary. Phrase it.\n\n"
                    + json.dumps(view, sort_keys=True, ensure_ascii=True)
                ),
            )
        ],
        # No tools. The narrative path has nothing to look up — everything it
        # may say is in the snapshot it was handed.
        declarations=[],
        model=model,
        timeout_seconds=settings.AI_TIMEOUT_SECONDS,
        max_output_tokens=400,
    )

    cost = engine.cost_for(model, turn.input_tokens, turn.output_tokens)
    await gateway.record_usage(
        session,
        tenant_id=tenant_id,
        calls=1,
        input_tokens=turn.input_tokens,
        output_tokens=turn.output_tokens,
        cost_inr=cost,
    )

    payload_hash = hashlib.sha256(
        json.dumps(view, sort_keys=True).encode("utf-8")
    ).hexdigest()

    async def record(outcome: str, error_code: str | None = None) -> None:
        await gateway.record_generation(
            session,
            tenant_id=tenant_id,
            purpose="narrative",
            model_name=model,
            model_version=turn.model_version,
            payload_hash=payload_hash,
            fields_sent=sorted(view),
            input_tokens=turn.input_tokens,
            output_tokens=turn.output_tokens,
            cost_inr=cost,
            latency_ms=turn.latency_ms,
            outcome=outcome,
            error_code=error_code,
        )

    if turn.error_code is not None:
        await record(gateway.Outcome.PROVIDER_ERROR, turn.error_code)
        return None

    if turn.tool_calls:
        # It was given no tools. A tool call here means the model is not doing
        # what it was asked, and the answer is not worth checking.
        await record(gateway.Outcome.OUTPUT_REJECTED, "unexpected_tool_call")
        return None

    prose = validate_prose(turn.text)
    if not prose.ok:
        await gateway.record_usage(session, tenant_id=tenant_id, outputs_rejected=1)
        await gateway.record_incident(
            session,
            tenant_id=tenant_id,
            kind=(
                PrivacyIncidentKind.OUTPUT_PII_ECHO
                if prose.rejected_by == "pii_echo"
                else PrivacyIncidentKind.OUTPUT_SCHEMA_VIOLATION
            ),
            detector=prose.detector or prose.rejected_by,
            context={"stage": "narrative"},
        )
        await record(gateway.Outcome.OUTPUT_REJECTED, prose.rejected_by)
        return None

    trace = traceability.check(prose.text or "", traceability.allowed_figures(view))
    if not trace.ok:
        logger.warning(
            "narrative_untraceable_figure",
            stage="narrative",
            error_code="untraceable_figure",
            count=len(trace.findings),
        )
        await gateway.record_usage(session, tenant_id=tenant_id, outputs_rejected=1)
        await gateway.record_incident(
            session,
            tenant_id=tenant_id,
            kind=PrivacyIncidentKind.OUTPUT_UNTRACEABLE_FIGURE,
            detector=",".join(trace.kinds),
            context={"stage": "narrative", "count": len(trace.findings)},
        )
        await record(gateway.Outcome.UNTRACEABLE, "untraceable_figure")
        return None

    await session.execute(
        text(
            """
            UPDATE insight_snapshots
            SET narrative = :text,
                narrative_model = :model,
                narrative_generated_at = :now
            WHERE period_month = :month
            """
        ),
        {
            "text": prose.text,
            "model": model,
            "now": datetime.now(timezone.utc),
            "month": month.replace(day=1),
        },
    )
    await record(gateway.Outcome.OK)
    return prose.text

