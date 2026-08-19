"""The single door between this system and any language model.

Nothing calls a provider directly. Every AI request in the platform passes
through :func:`classify`, which is where the guarantees are enforced and, just
as importantly, where they are *counted* — the Privacy Center's numbers come
from here rather than from an estimate.

The sequence, and what each step protects against:

1. **Is AI even on?** With ``AI_ENABLED=false`` or no key, the answer is no and
   the caller falls back to deterministic categorisation. The product is fully
   functional in this state; it is the default.
2. **Budget.** A runaway loop should stop spending, not keep going.
3. **Injection guard** on the untrusted strings. Quarantined text skips the
   model entirely rather than being sanitised — rewriting an attack into
   something "safe" is a guess about what the attacker meant.
4. **Scrub and allow-list**, then re-scan the built payload. Fails closed.
5. **Call the provider**, which sees only the validated payload.
6. **Validate the response** independently, because a successful injection
   shows up in what comes back.

Every failure path routes the transaction to human review. None of them retries
with looser settings, and none of them logs the offending content — incidents
record which detector fired, never what it found.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.base import AIProvider
from app.ai.router import get_provider
from app.core.config import settings
from app.core.logging import get_logger
from app.models.enums import PrivacyIncidentKind
from app.observability import metrics
from app.privacy import injection_guard, scrubber
from app.privacy.allowlist import AIPayload
from app.privacy.output_validator import VALID_CATEGORIES, ValidatedPrediction, validate

logger = get_logger(__name__)

#: Sorted so the prompt and the schema are stable across runs — an unstable
#: category order changes the payload hash and defeats repeat detection.
CATEGORY_ENUM: tuple[str, ...] = tuple(sorted(VALID_CATEGORIES))


class Outcome:
    """Why a call did or did not happen. Stored on every record."""

    OK = "ok"
    DISABLED = "ai_disabled"
    NO_PAYLOAD = "no_sendable_payload"
    QUARANTINED = "injection_quarantined"
    PII_BLOCKED = "pii_in_payload"
    BUDGET = "budget_exceeded"
    PROVIDER_ERROR = "provider_error"
    OUTPUT_REJECTED = "output_rejected"
    #: Assistant only: the answer quoted a figure no tool produced.
    UNTRACEABLE = "untraceable_figure"
    #: Assistant only: the model kept asking for tools past its budget.
    TOOL_BUDGET = "tool_budget_exceeded"


@dataclass(slots=True)
class GatewayResult:
    prediction: ValidatedPrediction | None
    outcome: str
    #: Field names actually sent, for the per-call record and the UI.
    fields_sent: list[str] | None = None
    detector: str | None = None
    model_name: str | None = None
    cost_inr: Decimal = Decimal("0")

    @property
    def ok(self) -> bool:
        return self.prediction is not None


def _payload_hash(payload: AIPayload) -> str:
    """Identifies a repeat call without storing what was sent."""
    body = json.dumps(payload.as_prompt_fields(), sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


async def _bump_counter(
    session: AsyncSession, *, tenant_id: uuid.UUID, **increments: object
) -> None:
    """Upsert today's rollup.

    A counter table rather than a query over incidents: the honest denominator —
    how many calls were actually made — is not derivable from a table that only
    records the ones that were blocked.
    """
    columns = (
        "ai_calls_made", "payloads_blocked", "injections_quarantined",
        "outputs_rejected", "input_tokens", "output_tokens", "cost_inr",
    )
    values = {column: increments.get(column, 0) for column in columns}

    await session.execute(
        text(
            """
            INSERT INTO privacy_counters (
                tenant_id, day, ai_calls_made, payloads_blocked,
                injections_quarantined, outputs_rejected,
                input_tokens, output_tokens, cost_inr, updated_at
            ) VALUES (
                :tenant_id, :day, :ai_calls_made, :payloads_blocked,
                :injections_quarantined, :outputs_rejected,
                :input_tokens, :output_tokens, :cost_inr, now()
            )
            ON CONFLICT (tenant_id, day) DO UPDATE SET
                ai_calls_made = privacy_counters.ai_calls_made + EXCLUDED.ai_calls_made,
                payloads_blocked = privacy_counters.payloads_blocked + EXCLUDED.payloads_blocked,
                injections_quarantined = privacy_counters.injections_quarantined
                    + EXCLUDED.injections_quarantined,
                outputs_rejected = privacy_counters.outputs_rejected + EXCLUDED.outputs_rejected,
                input_tokens = privacy_counters.input_tokens + EXCLUDED.input_tokens,
                output_tokens = privacy_counters.output_tokens + EXCLUDED.output_tokens,
                cost_inr = privacy_counters.cost_inr + EXCLUDED.cost_inr,
                updated_at = now()
            """
        ),
        {"tenant_id": tenant_id, "day": date.today(), **values},
    )


async def _record_incident(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    kind: PrivacyIncidentKind,
    transaction_id: uuid.UUID | None,
    detector: str | None = None,
    field_name: str | None = None,
    context: dict | None = None,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO privacy_incidents (
                tenant_id, transaction_id, kind, detector, field_name,
                provider, model_name, context
            ) VALUES (
                :tenant_id, :transaction_id, :kind, :detector, :field_name,
                :provider, :model_name, CAST(:context AS jsonb)
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "transaction_id": transaction_id,
            "kind": str(kind),
            "detector": detector,
            "field_name": field_name,
            "provider": settings.AI_PROVIDER,
            "model_name": settings.AI_MODEL_CATEGORIZE,
            "context": json.dumps(context or {}),
        },
    )


async def _month_spend(session: AsyncSession) -> Decimal:
    row = (
        await session.execute(
            text(
                "SELECT COALESCE(SUM(cost_inr), 0) AS spent FROM privacy_counters "
                "WHERE day >= date_trunc('month', CURRENT_DATE)"
            )
        )
    ).one()
    return Decimal(str(row.spent))


async def classify(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    transaction_id: uuid.UUID | None,
    merchant: str | None,
    merchant_is_known: bool,
    description: str,
    amount: Decimal,
    direction: str,
    payment_method: str,
    mcc: str | None = None,
    txn_date: date | None = None,
    provider: AIProvider | None = None,
) -> GatewayResult:
    """Categorise one transaction, or explain why we did not."""
    if not settings.ai_usable:
        return GatewayResult(prediction=None, outcome=Outcome.DISABLED)

    # --- 1. injection guard --------------------------------------------------
    # Runs on the *raw* strings, before anything is trimmed — an attack hidden in
    # a field we later discard is still evidence the statement is hostile.
    verdict = injection_guard.inspect(merchant, description)
    if verdict.quarantined:
        await _record_incident(
            session, tenant_id=tenant_id, transaction_id=transaction_id,
            kind=PrivacyIncidentKind.INJECTION_QUARANTINED,
            detector=verdict.reason, field_name="merchant_or_description",
        )
        await _bump_counter(session, tenant_id=tenant_id, injections_quarantined=1)
        metrics.prompt_injection_quarantined_total.inc()
        logger.warning(
            "ai_injection_quarantined", stage="privacy", error_code=verdict.reason
        )
        return GatewayResult(
            prediction=None, outcome=Outcome.QUARANTINED, detector=verdict.reason
        )

    # --- 2. build and verify the payload ------------------------------------
    scrubbed = scrubber.build_payload(
        merchant=merchant,
        merchant_is_known=merchant_is_known,
        description=description,
        amount=amount,
        direction=direction,
        payment_method=payment_method,
        mcc=mcc,
        txn_date=txn_date,
    )
    if not scrubbed.ok:
        if scrubbed.blocked_by in {"no_sendable_merchant", "schema_violation"}:
            # Not an incident: nothing was withheld in error, there was simply
            # nothing safe to ask about.
            return GatewayResult(prediction=None, outcome=Outcome.NO_PAYLOAD)

        await _record_incident(
            session, tenant_id=tenant_id, transaction_id=transaction_id,
            kind=PrivacyIncidentKind.PII_IN_PAYLOAD,
            detector=scrubbed.blocked_by, field_name=scrubbed.blocked_field,
        )
        await _bump_counter(session, tenant_id=tenant_id, payloads_blocked=1)
        metrics.privacy_payloads_blocked_total.labels(
            detector=scrubbed.blocked_by or "unknown"
        ).inc()
        logger.error(
            "ai_payload_blocked", stage="privacy", error_code=scrubbed.blocked_by
        )
        return GatewayResult(
            prediction=None, outcome=Outcome.PII_BLOCKED, detector=scrubbed.blocked_by
        )

    payload = scrubbed.payload
    assert payload is not None

    # --- 3. budget -----------------------------------------------------------
    if await _month_spend(session) >= settings.AI_MONTHLY_BUDGET_INR:
        await _record_incident(
            session, tenant_id=tenant_id, transaction_id=transaction_id,
            kind=PrivacyIncidentKind.BUDGET_EXCEEDED,
        )
        return GatewayResult(prediction=None, outcome=Outcome.BUDGET)

    # --- 4. the call ---------------------------------------------------------
    engine = provider or get_provider()
    if not engine.available():
        return GatewayResult(prediction=None, outcome=Outcome.DISABLED)

    model = settings.AI_MODEL_CATEGORIZE
    response = await engine.classify(
        payload, model=model, categories=CATEGORY_ENUM,
        timeout_seconds=settings.AI_TIMEOUT_SECONDS,
    )
    cost = engine.cost_for(model, response.input_tokens, response.output_tokens)

    await _bump_counter(
        session, tenant_id=tenant_id, ai_calls_made=1,
        input_tokens=response.input_tokens, output_tokens=response.output_tokens,
        cost_inr=cost,
    )
    _observe_call(
        model=model, purpose="categorize", response=response, cost=cost,
        outcome=Outcome.OK if response.ok else Outcome.PROVIDER_ERROR,
    )

    if not response.ok:
        await _record_call(
            session, tenant_id=tenant_id, transaction_id=transaction_id,
            payload=payload, response=response, cost=cost,
            outcome=Outcome.PROVIDER_ERROR, prediction=None,
        )
        return GatewayResult(
            prediction=None, outcome=Outcome.PROVIDER_ERROR,
            fields_sent=payload.field_names(), model_name=model, cost_inr=cost,
        )

    # --- 5. distrust the answer ---------------------------------------------
    validated = validate(response.raw)
    if not validated.ok:
        await _record_incident(
            session, tenant_id=tenant_id, transaction_id=transaction_id,
            kind=(
                PrivacyIncidentKind.OUTPUT_PII_ECHO
                if validated.rejected_by == "pii_echo"
                else PrivacyIncidentKind.OUTPUT_SCHEMA_VIOLATION
            ),
            detector=validated.detector or validated.rejected_by,
            context={"stage": "output"},
        )
        await _bump_counter(session, tenant_id=tenant_id, outputs_rejected=1)
        metrics.ai_calls_total.labels(
            provider=settings.AI_PROVIDER, model_name=model,
            purpose="categorize", outcome=Outcome.OUTPUT_REJECTED,
        ).inc()
        await _record_call(
            session, tenant_id=tenant_id, transaction_id=transaction_id,
            payload=payload, response=response, cost=cost,
            outcome=Outcome.OUTPUT_REJECTED, prediction=None,
        )
        logger.warning(
            "ai_output_rejected", stage="privacy", error_code=validated.rejected_by
        )
        return GatewayResult(
            prediction=None, outcome=Outcome.OUTPUT_REJECTED,
            detector=validated.rejected_by, fields_sent=payload.field_names(),
            model_name=model, cost_inr=cost,
        )

    await _record_call(
        session, tenant_id=tenant_id, transaction_id=transaction_id,
        payload=payload, response=response, cost=cost,
        outcome=Outcome.OK, prediction=validated.prediction,
    )

    return GatewayResult(
        prediction=validated.prediction,
        outcome=Outcome.OK,
        fields_sent=payload.field_names(),
        model_name=model,
        cost_inr=cost,
    )


def _observe_call(
    *, model: str, purpose: str, response, cost: Decimal, outcome: str
) -> None:
    """Prometheus's view of one provider call.

    Labels are provider, model, purpose and outcome — four low-cardinality
    shapes. Deliberately *not* tenant: a per-tenant label on an AI counter
    would put the customer list in an endpoint that is unauthenticated by
    convention, and would make the series count grow with the business.
    """
    provider_name = settings.AI_PROVIDER
    metrics.ai_calls_total.labels(
        provider=provider_name, model_name=model, purpose=purpose, outcome=outcome
    ).inc()
    metrics.ai_call_duration_seconds.labels(
        provider=provider_name, model_name=model
    ).observe(response.latency_ms / 1000)
    if cost:
        metrics.ai_cost_inr_total.labels(
            provider=provider_name, model_name=model
        ).inc(float(cost))
    for direction, tokens in (
        ("input", response.input_tokens), ("output", response.output_tokens)
    ):
        if tokens:
            metrics.ai_tokens_total.labels(
                provider=provider_name, model_name=model, direction=direction
            ).inc(tokens)


async def _record_call(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    transaction_id: uuid.UUID | None,
    payload: AIPayload,
    response,
    cost: Decimal,
    outcome: str,
    prediction: ValidatedPrediction | None,
) -> None:
    """One row per call. Field *names* and a payload hash — never content."""
    category_id = None
    if prediction is not None:
        row = (
            await session.execute(
                text("SELECT id FROM categories WHERE slug = :slug"),
                {"slug": prediction.category_slug},
            )
        ).one_or_none()
        category_id = row.id if row else None

    await session.execute(
        text(
            """
            INSERT INTO ai_classifications (
                tenant_id, transaction_id, provider, model_name, model_version,
                purpose, payload_hash, fields_sent, predicted_category_id,
                predicted_confidence, accepted, input_tokens, output_tokens,
                cost_inr, latency_ms, outcome, error_code
            ) VALUES (
                :tenant_id, :transaction_id, :provider, :model_name, :model_version,
                'categorize', :payload_hash, CAST(:fields_sent AS jsonb), :category_id,
                :confidence, :accepted, :input_tokens, :output_tokens,
                :cost_inr, :latency_ms, :outcome, :error_code
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "transaction_id": transaction_id,
            "provider": settings.AI_PROVIDER,
            "model_name": response.model_name,
            "model_version": response.model_version,
            "payload_hash": _payload_hash(payload),
            "fields_sent": json.dumps(payload.field_names()),
            "category_id": category_id,
            "confidence": prediction.confidence if prediction else None,
            "accepted": prediction is not None,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "cost_inr": cost,
            "latency_ms": response.latency_ms,
            "outcome": outcome,
            "error_code": response.error_code,
        },
    )


# --------------------------------------------------------------------------- #
# Bookkeeping shared with the assistant path
# --------------------------------------------------------------------------- #
#
# The assistant does not go through `classify` — it is a conversation, not a
# single structured call — but it must land in the same ledgers, or the Privacy
# Center reports one number and reality is another. These are the counter, the
# incident log and the budget, exposed so there is exactly one implementation of
# each rather than a second copy that drifts.


async def record_usage(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    calls: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_inr: Decimal = Decimal("0"),
    outputs_rejected: int = 0,
    payloads_blocked: int = 0,
) -> None:
    """Add one call's usage to today's rollup."""
    await _bump_counter(
        session,
        tenant_id=tenant_id,
        ai_calls_made=calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_inr=cost_inr,
        outputs_rejected=outputs_rejected,
        payloads_blocked=payloads_blocked,
    )


async def record_incident(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    kind: PrivacyIncidentKind,
    detector: str | None = None,
    field_name: str | None = None,
    context: dict | None = None,
) -> None:
    """Log a privacy or trust failure. Never the content that caused it."""
    await _record_incident(
        session,
        tenant_id=tenant_id,
        kind=kind,
        transaction_id=None,
        detector=detector,
        field_name=field_name,
        context=context,
    )


async def within_budget(session: AsyncSession) -> bool:
    """Whether this month's spend is still under the configured ceiling."""
    return await _month_spend(session) < settings.AI_MONTHLY_BUDGET_INR


async def record_generation(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    purpose: str,
    model_name: str,
    model_version: str | None,
    payload_hash: str,
    fields_sent: list[str],
    input_tokens: int,
    output_tokens: int,
    cost_inr: Decimal,
    latency_ms: int,
    outcome: str,
    error_code: str | None = None,
) -> None:
    """One row per generation call, whatever its purpose.

    ``fields_sent`` records the *names* of what crossed the perimeter — for the
    assistant, the tools that ran. Never their results.
    """
    metrics.ai_calls_total.labels(
        provider=settings.AI_PROVIDER, model_name=model_name,
        purpose=purpose, outcome=outcome,
    ).inc()
    metrics.ai_call_duration_seconds.labels(
        provider=settings.AI_PROVIDER, model_name=model_name
    ).observe(latency_ms / 1000)
    if cost_inr:
        metrics.ai_cost_inr_total.labels(
            provider=settings.AI_PROVIDER, model_name=model_name
        ).inc(float(cost_inr))
    for direction, tokens in (("input", input_tokens), ("output", output_tokens)):
        if tokens:
            metrics.ai_tokens_total.labels(
                provider=settings.AI_PROVIDER, model_name=model_name,
                direction=direction,
            ).inc(tokens)

    await session.execute(
        text(
            """
            INSERT INTO ai_classifications (
                tenant_id, transaction_id, provider, model_name, model_version,
                purpose, payload_hash, fields_sent, accepted,
                input_tokens, output_tokens, cost_inr, latency_ms,
                outcome, error_code
            ) VALUES (
                :tenant_id, NULL, :provider, :model_name, :model_version,
                :purpose, :payload_hash, CAST(:fields_sent AS jsonb), :accepted,
                :input_tokens, :output_tokens, :cost_inr, :latency_ms,
                :outcome, :error_code
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "provider": settings.AI_PROVIDER,
            "model_name": model_name,
            "model_version": model_version,
            "purpose": purpose,
            "payload_hash": payload_hash,
            "fields_sent": json.dumps(fields_sent),
            "accepted": outcome == Outcome.OK,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_inr": cost_inr,
            "latency_ms": latency_ms,
            "outcome": outcome,
            "error_code": error_code,
        },
    )
