"""Audit trail, notifications and extraction accuracy history."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    Base,
    MediumStr,
    TenantScopedMixin,
    TimestampMixin,
    enum_check,
    uuid_pk,
)
from app.models.enums import AccuracyCorpus, AuditAction, NotificationKind


class AuditLog(Base, TenantScopedMixin):
    """Append-only record of who did what.

    ``details`` is JSONB and it is tempting to put everything in it. It must
    carry only non-sensitive context — field *names* that changed, counts,
    ids — never the values. This table is read by users on the Audit screen and
    exported on request; an amount stored here is an amount that leaves.
    """

    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    action: Mapped[str] = mapped_column(String(48), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(48))
    resource_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))

    # Correlates an audit entry with the request that produced it, so a support
    # question can be traced through the logs without guessing.
    request_id: Mapped[str | None] = mapped_column(String(64))
    ip_address: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(String(255))

    succeeded: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        enum_check("action", AuditAction),
        Index("ix_audit_logs_tenant_occurred", "tenant_id", "occurred_at"),
        Index("ix_audit_logs_tenant_action", "tenant_id", "action"),
        Index("ix_audit_logs_user", "user_id", "occurred_at"),
        Index("ix_audit_logs_resource", "resource_type", "resource_id"),
    )


class Notification(Base, TenantScopedMixin, TimestampMixin):
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )

    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    title: Mapped[MediumStr]
    body: Mapped[str | None] = mapped_column(Text)

    # Deep link into the relevant statement, transaction or budget.
    resource_type: Mapped[str | None] = mapped_column(String(48))
    resource_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))

    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        enum_check("kind", NotificationKind),
        Index("ix_notifications_tenant_created", "tenant_id", "created_at"),
        Index("ix_notifications_user_unread", "user_id", "read_at"),
    )


class ExtractionAccuracyRun(Base, TimestampMixin):
    """A scorecard from the accuracy harness.

    Global, not tenant-scoped: these runs score the parsers against fixtures,
    not anyone's data.

    The columns that matter most are ``missing_transactions`` and
    ``extra_transactions``. Field accuracy alone is easy to game — a harness
    that scores only the rows it managed to extract can report 99.9% amount
    accuracy while dropping 40% of a statement. Recall and precision are stored
    separately and never averaged into a single headline number.
    """

    __tablename__ = "extraction_accuracy_runs"

    id: Mapped[uuid.UUID] = uuid_pk()

    corpus: Mapped[str] = mapped_column(String(16), nullable=False)
    bank_code: Mapped[str | None] = mapped_column(String(32))
    fixture_name: Mapped[str | None] = mapped_column(String(255))
    parser_version: Mapped[str | None] = mapped_column(String(32))
    git_commit: Mapped[str | None] = mapped_column(String(40))

    # --- ground truth vs extracted -----------------------------------------
    expected_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    extracted_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    matched_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    missing_transactions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    extra_transactions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # --- field errors, counted over the ground-truth set --------------------
    # A missing transaction counts as an error in every one of these. That is
    # the rule that stops the scorecard flattering itself.
    wrong_date: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    wrong_amount: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    wrong_direction: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    wrong_merchant: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    wrong_category: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # --- rates --------------------------------------------------------------
    recall: Mapped[Decimal | None] = mapped_column(Numeric(6, 5))
    precision: Mapped[Decimal | None] = mapped_column(Numeric(6, 5))
    date_accuracy: Mapped[Decimal | None] = mapped_column(Numeric(6, 5))
    amount_accuracy: Mapped[Decimal | None] = mapped_column(Numeric(6, 5))
    direction_accuracy: Mapped[Decimal | None] = mapped_column(Numeric(6, 5))
    merchant_accuracy: Mapped[Decimal | None] = mapped_column(Numeric(6, 5))
    category_accuracy: Mapped[Decimal | None] = mapped_column(Numeric(6, 5))

    reconciled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Per-metric measured/target/passed detail, for the printed scorecard.
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (
        enum_check("corpus", AccuracyCorpus),
        Index("ix_extraction_accuracy_runs_corpus_created", "corpus", "created_at"),
        Index("ix_extraction_accuracy_runs_bank", "bank_code"),
    )
