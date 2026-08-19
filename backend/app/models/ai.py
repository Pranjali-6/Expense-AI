"""AI call records and privacy telemetry.

Note what is *not* here: there is no column holding a prompt, a payload or a
model response. Storing them would recreate, inside PostgreSQL, exactly the
data the privacy gateway exists to keep out of the model — and a table of
prompts is a table of transaction descriptions by another name.

What is recorded is the shape of the call: which model, how many tokens, what
it cost, how long it took, and a hash of the prompt for cache and drift
analysis. That is enough to audit spend and behaviour without keeping content.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    CONFIDENCE,
    Base,
    TenantScopedMixin,
    TimestampMixin,
    confidence_check,
    enum_check,
    uuid_pk,
)
from app.models.enums import PrivacyIncidentKind


class AIClassification(Base, TenantScopedMixin, TimestampMixin):
    __tablename__ = "ai_classifications"

    id: Mapped[uuid.UUID] = uuid_pk()
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("transactions.id", ondelete="CASCADE")
    )

    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model_name: Mapped[str] = mapped_column(String(64), nullable=False)
    model_version: Mapped[str | None] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)

    # Hash of the sanitised payload — enough to detect repeats and prompt drift,
    # useless to anyone who obtains it.
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Field *names* sent, never values. Renders the Privacy Center allow-list
    # from what actually happened rather than from a hardcoded list.
    fields_sent: Mapped[list[str] | None] = mapped_column(JSONB)

    predicted_category_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("categories.id", ondelete="SET NULL")
    )
    predicted_confidence: Mapped[Decimal | None] = mapped_column(CONFIDENCE)
    accepted: Mapped[bool] = mapped_column(default=False, nullable=False)

    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    # Rupees, six decimal places — a single classification costs a fraction of
    # a paisa and rounding to two would report every call as free.
    cost_inr: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    latency_ms: Mapped[int | None] = mapped_column(Integer)

    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        confidence_check("predicted_confidence"),
        Index("ix_ai_classifications_tenant_created", "tenant_id", "created_at"),
        Index("ix_ai_classifications_transaction", "transaction_id"),
        Index("ix_ai_classifications_model", "provider", "model_name"),
    )


class PrivacyIncident(Base, TenantScopedMixin, TimestampMixin):
    """A privacy control that fired.

    Every row here represents an AI call that did **not** happen, or a response
    that was rejected. The system fails closed: the transaction goes to human
    review rather than being retried with a cleaner payload.
    """

    __tablename__ = "privacy_incidents"

    id: Mapped[uuid.UUID] = uuid_pk()
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("transactions.id", ondelete="SET NULL")
    )

    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    # Which detector fired (PAN, CARD_NUMBER, UPI_ID…). The detector's *name*,
    # never the matched text — storing the evidence would be the leak.
    detector: Mapped[str | None] = mapped_column(String(48))
    field_name: Mapped[str | None] = mapped_column(String(64))

    provider: Mapped[str | None] = mapped_column(String(32))
    model_name: Mapped[str | None] = mapped_column(String(64))

    # Extra non-sensitive context: match counts, payload size, stage.
    context: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (
        enum_check("kind", PrivacyIncidentKind),
        Index("ix_privacy_incidents_tenant_created", "tenant_id", "created_at"),
        Index("ix_privacy_incidents_tenant_kind", "tenant_id", "kind"),
    )


class PrivacyCounter(Base, TenantScopedMixin):
    """Daily rollup powering the Privacy Center.

    A counter table rather than a query over incidents, because the honest
    denominator — how many calls were *made* — is not derivable from a table of
    calls that were blocked.
    """

    __tablename__ = "privacy_counters"

    id: Mapped[uuid.UUID] = uuid_pk()
    day: Mapped[date] = mapped_column(Date, nullable=False)

    ai_calls_made: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    payloads_blocked: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    injections_quarantined: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    outputs_rejected: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_inr: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), default=0, nullable=False
    )

    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "day", name="uq_privacy_counters_tenant_id_day"),
        Index("ix_privacy_counters_tenant_day", "tenant_id", "day"),
    )
