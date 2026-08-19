"""Outputs of the Financial Intelligence Engine.

Every row in these tables is produced by deterministic Python and SQL. No LLM
writes here, and no LLM reads anything but the finished aggregates. Precomputing
by scheduler keeps the dashboard fast and, more importantly, means the assistant
answers from settled numbers rather than recomputing under a chat request.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    CONFIDENCE,
    MONEY,
    Base,
    MediumStr,
    TenantScopedMixin,
    TimestampMixin,
    confidence_check,
    enum_check,
    uuid_pk,
)
from app.models.enums import (
    AnomalyKind,
    BudgetPeriod,
    SubscriptionCadence,
    SubscriptionStatus,
    TimelineEventKind,
)


class Subscription(Base, TenantScopedMixin, TimestampMixin):
    """A recurring charge detected from transaction history.

    Detected, never entered: merchants are grouped, the intervals between their
    charges are clustered, and a cadence with a stability score falls out. A
    user adding subscriptions by hand would defeat the point.
    """

    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = uuid_pk()
    merchant: Mapped[MediumStr]
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("categories.id", ondelete="SET NULL")
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL")
    )

    cadence: Mapped[str] = mapped_column(String(32), nullable=False)
    # How regular the intervals are. A wobbly cadence is still reported, but
    # with a score that says so rather than being presented as certain.
    cadence_stability: Mapped[Decimal] = mapped_column(CONFIDENCE, nullable=False)

    typical_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    last_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    estimated_annual_cost: Mapped[Decimal] = mapped_column(MONEY, nullable=False)

    first_charge_on: Mapped[date] = mapped_column(Date, nullable=False)
    last_charge_on: Mapped[date] = mapped_column(Date, nullable=False)
    next_expected_on: Mapped[date | None] = mapped_column(Date)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    status: Mapped[str] = mapped_column(
        String(32), default=SubscriptionStatus.ACTIVE, nullable=False
    )
    # A user can dismiss a false positive without it being re-detected.
    dismissed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "merchant", "cadence",
            name="uq_subscriptions_tenant_id_merchant_cadence",
        ),
        enum_check("cadence", SubscriptionCadence),
        enum_check("status", SubscriptionStatus),
        confidence_check("cadence_stability"),
        Index("ix_subscriptions_tenant_status", "tenant_id", "status"),
        Index("ix_subscriptions_tenant_next", "tenant_id", "next_expected_on"),
    )


class Budget(Base, TenantScopedMixin, TimestampMixin):
    """An optional spending target for a category."""

    __tablename__ = "budgets"

    id: Mapped[uuid.UUID] = uuid_pk()
    category_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("categories.id", ondelete="CASCADE"), nullable=False
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    period: Mapped[str] = mapped_column(
        String(32), default=BudgetPeriod.MONTHLY, nullable=False
    )

    starts_on: Mapped[date] = mapped_column(Date, nullable=False)
    ends_on: Mapped[date | None] = mapped_column(Date)

    # Fraction of the budget at which to notify. 0.8 = warn at 80%.
    alert_threshold: Mapped[Decimal] = mapped_column(
        CONFIDENCE, default=Decimal("0.800"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "category_id", "period", "starts_on",
            name="uq_budgets_tenant_id_category_id_period_starts_on",
        ),
        enum_check("period", BudgetPeriod),
        confidence_check("alert_threshold"),
        Index("ix_budgets_tenant_active", "tenant_id", "is_active"),
    )


class InsightSnapshot(Base, TenantScopedMixin, TimestampMixin):
    """A month's finished figures.

    Computed by the scheduler from reconciled transactions. The AI, when
    enabled, receives *this* — already rounded, already aggregated — and turns
    it into prose. When AI is off the same snapshot renders as structured cards,
    which is why the product loses nothing without a key.
    """

    __tablename__ = "insight_snapshots"

    id: Mapped[uuid.UUID] = uuid_pk()
    # First day of the month the snapshot describes.
    period_month: Mapped[date] = mapped_column(Date, nullable=False)

    total_expenses: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    total_income: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    net_cash_flow: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    savings_rate: Mapped[Decimal] = mapped_column(CONFIDENCE, nullable=False)

    largest_category_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("categories.id", ondelete="SET NULL")
    )
    fastest_growing_category_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("categories.id", ondelete="SET NULL")
    )
    largest_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("transactions.id", ondelete="SET NULL")
    )

    transaction_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Transactions still awaiting review when this was computed. Surfaced with
    # the figures, because a total drawn from a partly unreviewed month should
    # say so rather than present itself as final.
    unreviewed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Statements covering this month that failed reconciliation.
    untrusted_statement_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )

    # Category rollups, top merchants, daily series — the full computed payload.
    breakdown: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    # Optional AI phrasing of the above. Null when AI is disabled; the UI reads
    # `breakdown` either way.
    narrative: Mapped[str | None] = mapped_column(Text)
    narrative_model: Mapped[str | None] = mapped_column(String(64))
    narrative_generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "period_month", name="uq_insight_snapshots_tenant_id_period_month"
        ),
        confidence_check("savings_rate"),
        Index("ix_insight_snapshots_tenant_month", "tenant_id", "period_month"),
    )


class Anomaly(Base, TenantScopedMixin, TimestampMixin):
    """A statistical outlier, with the numbers that make it one.

    Never described as fraud. The system has no ground truth for that, and every
    kind here has ordinary explanations — a spike in Travel is usually a
    holiday. ``reason`` carries the actual figures so the user can judge.
    """

    __tablename__ = "anomalies"

    id: Mapped[uuid.UUID] = uuid_pk()
    kind: Mapped[str] = mapped_column(String(48), nullable=False)

    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("transactions.id", ondelete="CASCADE")
    )
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("categories.id", ondelete="SET NULL")
    )

    detected_on: Mapped[date] = mapped_column(Date, nullable=False)
    period_month: Mapped[date | None] = mapped_column(Date)

    observed_value: Mapped[Decimal | None] = mapped_column(MONEY)
    baseline_value: Mapped[Decimal | None] = mapped_column(MONEY)
    # Robust z-score (median + MAD), which is not fooled by the outlier itself
    # the way a mean-and-standard-deviation score is.
    deviation_score: Mapped[Decimal | None] = mapped_column(CONFIDENCE)

    # Plain sentence with the real numbers in it.
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    dismissed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    __table_args__ = (
        enum_check("kind", AnomalyKind),
        Index("ix_anomalies_tenant_detected", "tenant_id", "detected_on"),
        Index("ix_anomalies_tenant_kind", "tenant_id", "kind"),
        Index("ix_anomalies_transaction", "transaction_id"),
    )


class TimelineEvent(Base, TenantScopedMixin, TimestampMixin):
    """One entry on the Financial Timeline.

    Materialised rather than assembled on read: the timeline unions
    transactions, imports, budget breaches, renewals and anomalies, and doing
    that as a live query across five tables per scroll would be slow and
    fragile.
    """

    __tablename__ = "timeline_events"

    id: Mapped[uuid.UUID] = uuid_pk()
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    occurred_on: Mapped[date] = mapped_column(Date, nullable=False)

    title: Mapped[MediumStr]
    summary: Mapped[str | None] = mapped_column(Text)
    amount: Mapped[Decimal | None] = mapped_column(MONEY)

    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("transactions.id", ondelete="CASCADE")
    )
    statement_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("statements.id", ondelete="CASCADE")
    )
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("categories.id", ondelete="SET NULL")
    )

    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (
        enum_check("kind", TimelineEventKind),
        Index("ix_timeline_events_tenant_occurred", "tenant_id", "occurred_on"),
        Index("ix_timeline_events_tenant_kind", "tenant_id", "kind"),
    )
