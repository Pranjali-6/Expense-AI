"""Statements, their pages, and the health report that decides trust."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
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
from app.models.enums import (
    DocumentType,
    ExtractionMethod,
    StatementStatus,
    TrustStatus,
)


class Statement(Base, TenantScopedMixin, TimestampMixin):
    __tablename__ = "statements"

    id: Mapped[uuid.UUID] = uuid_pk()
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL")
    )
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    # --- source object ------------------------------------------------------
    # The object key carries ids only. An original filename can contain a name,
    # an account number or a period, so it is never persisted.
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    # --- what we worked out about it ---------------------------------------
    document_type: Mapped[str] = mapped_column(
        String(32), default=DocumentType.UNKNOWN, nullable=False
    )
    bank_code: Mapped[str | None] = mapped_column(String(32))
    bank_detection_confidence: Mapped[Decimal | None] = mapped_column(CONFIDENCE)
    parser_name: Mapped[str | None] = mapped_column(String(64))
    parser_version: Mapped[str | None] = mapped_column(String(32))

    # --- statement metadata as printed -------------------------------------
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    opening_balance: Mapped[Decimal | None] = mapped_column()
    closing_balance: Mapped[Decimal | None] = mapped_column()
    account_last4: Mapped[str | None] = mapped_column(String(4))

    page_count: Mapped[int | None] = mapped_column(Integer)
    extraction_method: Mapped[str | None] = mapped_column(String(32))

    # --- state --------------------------------------------------------------
    status: Mapped[str] = mapped_column(
        String(32), default=StatementStatus.UPLOADED, nullable=False
    )

    # Reaches `trusted` only on an exact zero reconciliation delta. Analytics
    # and AI narrative inputs both key off this.
    trust_status: Mapped[str] = mapped_column(
        String(32), default=TrustStatus.PENDING, nullable=False
    )

    transaction_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Machine-readable only. Never a raw exception message: those carry values.
    error_code: Mapped[str | None] = mapped_column(String(64))

    # Failed password attempts against a `password_required` statement.
    #
    # Persisted rather than held in Redis because it is a security control, and
    # a cache flush must not hand an attacker a fresh budget. Indian bank
    # statement passwords follow published per-bank formulas over PAN, date of
    # birth and account digits, so an uncapped unlock endpoint is a practical
    # oracle against exactly the identifiers this system exists to protect.
    unlock_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # The same PDF uploaded twice is the single most common way duplicate
        # transactions appear. Catch it at the door, by content hash, before
        # any extraction work happens.
        #
        # Partial, on purpose: a soft-deleted statement must release its hash,
        # or deleting a bad import and re-uploading the corrected file — the
        # obvious recovery path — is permanently blocked by a row the user
        # believes they removed.
        Index(
            "uq_statements_tenant_id_file_sha256",
            "tenant_id",
            "file_sha256",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        enum_check("document_type", DocumentType),
        enum_check("status", StatementStatus),
        enum_check("trust_status", TrustStatus),
        enum_check("extraction_method", ExtractionMethod, nullable=True),
        Index("ix_statements_tenant_status", "tenant_id", "status"),
        Index("ix_statements_tenant_account", "tenant_id", "account_id"),
        Index("ix_statements_tenant_period", "tenant_id", "period_start", "period_end"),
        Index("ix_statements_tenant_trust", "tenant_id", "trust_status"),
    )


class StatementPage(Base, TenantScopedMixin):
    """Per-page extraction record.

    Kept so a transaction can be traced to the page it came from, and so the
    OCR ratio on the health report is measured rather than estimated.
    """

    __tablename__ = "statement_pages"

    id: Mapped[uuid.UUID] = uuid_pk()
    statement_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("statements.id", ondelete="CASCADE"),
        nullable=False,
    )

    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    extraction_method: Mapped[str] = mapped_column(String(32), nullable=False)

    char_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    table_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # OCR's own per-page confidence, when OCR ran. Feeds confidence_extraction.
    ocr_confidence: Mapped[Decimal | None] = mapped_column(CONFIDENCE)

    __table_args__ = (
        UniqueConstraint(
            "statement_id", "page_number", name="uq_statement_pages_statement_id_page_number"
        ),
        enum_check("extraction_method", ExtractionMethod),
        confidence_check("ocr_confidence"),
        Index("ix_statement_pages_tenant_statement", "tenant_id", "statement_id"),
    )


class StatementHealth(Base, TenantScopedMixin, TimestampMixin):
    """Whether an import can be trusted, and if not, exactly where it broke.

    This is the record behind the Statement Health screen. It exists as its own
    table rather than as columns on ``statements`` because it is a *report* —
    regenerated wholesale on every reprocess, and read as a unit.
    """

    __tablename__ = "statement_health"

    id: Mapped[uuid.UUID] = uuid_pk()
    statement_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("statements.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )

    # --- reconciliation -----------------------------------------------------
    # opening + credits - debits - closing. Stored in paise as an exact integer
    # so "is it zero?" is an integer comparison, never a float epsilon dance.
    reconciliation_delta_paise: Mapped[int | None] = mapped_column(BigInteger)
    reconciles: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    total_debits: Mapped[Decimal | None] = mapped_column()
    total_credits: Mapped[Decimal | None] = mapped_column()

    # --- continuity ---------------------------------------------------------
    balance_continuous: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # The row where the running balance first stopped following. This is the
    # single most useful number for debugging a parser.
    first_divergent_row: Mapped[int | None] = mapped_column(Integer)
    first_divergent_page: Mapped[int | None] = mapped_column(Integer)

    pages_continuous: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    missing_pages: Mapped[list[str] | None] = mapped_column(JSONB)

    # --- counts -------------------------------------------------------------
    declared_transaction_count: Mapped[int | None] = mapped_column(Integer)
    extracted_transaction_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )

    # --- extraction quality -------------------------------------------------
    ocr_page_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_page_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    avg_confidence_extraction: Mapped[Decimal | None] = mapped_column(CONFIDENCE)
    avg_confidence_merchant: Mapped[Decimal | None] = mapped_column(CONFIDENCE)
    avg_confidence_category: Mapped[Decimal | None] = mapped_column(CONFIDENCE)
    avg_confidence_validation: Mapped[Decimal | None] = mapped_column(CONFIDENCE)

    # Per-check pass/fail detail, rendered as the health table in the UI.
    checks: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (
        Index("ix_statement_health_tenant_statement", "tenant_id", "statement_id"),
        Index("ix_statement_health_tenant_reconciles", "tenant_id", "reconciles"),
    )
