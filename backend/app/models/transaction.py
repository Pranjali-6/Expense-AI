"""The trusted ledger.

Three design decisions here carry most of the system's integrity guarantees.

**Originals are frozen.** Every ``original_*`` column records exactly what came
off the statement and is never updated — enforced by a database trigger, not by
convention. When a user corrects something, the correction lands in a
``corrected_*`` column beside it. The value the application reads is a
PostgreSQL ``GENERATED`` column computing ``COALESCE(corrected, original)``, so
the effective value can never disagree with the coalesce rule: there is no code
path that could set them inconsistently, because there is no code path that sets
the effective value at all.

**Confidence is four numbers, gated on the minimum.** A single blended score
hides the failure that matters — a perfectly categorised transaction sitting on
a misread amount averages out to "fine". ``confidence_min`` is generated as
``LEAST(...)`` of the four, so the gate cannot drift from the definition.

**The fingerprint deliberately excludes the running balance.** The same
transaction legitimately carries different balances across re-issued, corrected
or overlapping statements. Folding balance into the key would produce a
different hash for an identical transaction and silently defeat deduplication —
which is precisely the failure this ledger must not have. Balance is corroborating
evidence and a confidence signal, never identity.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
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
    TenantScopedMixin,
    TimestampMixin,
    confidence_check,
    enum_check,
    uuid_pk,
)
from app.models.enums import (
    CategorySource,
    Direction,
    MovementType,
    PaymentMethod,
    ReviewStatus,
)


class TransferGroup(Base, TenantScopedMixin, TimestampMixin):
    """Links the two sides of an internal money movement.

    A ₹50,000 debit from savings and the matching ₹50,000 credit on the current
    account are one movement, not ₹100,000 of activity. Both rows get the same
    ``transfer_group_id`` and ``is_expense = false``.
    """

    __tablename__ = "transfer_groups"

    id: Mapped[uuid.UUID] = uuid_pk()
    movement_type: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    detected_on: Mapped[date] = mapped_column(Date, nullable=False)

    # How sure we are that these two rows really are the same movement.
    match_confidence: Mapped[Decimal] = mapped_column(CONFIDENCE, nullable=False)
    # e.g. {"date_gap_days": 1, "same_amount": true, "counterparty": "matched"}
    match_evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (
        enum_check("movement_type", MovementType),
        confidence_check("match_confidence"),
        Index("ix_transfer_groups_tenant_date", "tenant_id", "detected_on"),
    )


class Transaction(Base, TenantScopedMixin, TimestampMixin):
    __tablename__ = "transactions"

    id: Mapped[uuid.UUID] = uuid_pk()

    account_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    # Nullable so a future non-PDF ingestion source can write to the same ledger.
    statement_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("statements.id", ondelete="CASCADE")
    )

    # ---------------------------------------------------------------- frozen --
    # Never updated after insert. The freeze trigger rejects any UPDATE that
    # touches one of these.
    original_txn_date: Mapped[date] = mapped_column(Date, nullable=False)
    original_value_date: Mapped[date | None] = mapped_column(Date)
    original_description: Mapped[str] = mapped_column(Text, nullable=False)
    # Always positive. Sign is carried by `direction`, so a negative amount is
    # never a thing the rest of the system has to reason about.
    original_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    original_direction: Mapped[str] = mapped_column(String(16), nullable=False)
    original_balance_after: Mapped[Decimal | None] = mapped_column(MONEY)
    original_reference: Mapped[str | None] = mapped_column(String(128))
    original_merchant: Mapped[str | None] = mapped_column(String(255))
    original_payment_method: Mapped[str] = mapped_column(
        String(32), default=PaymentMethod.UNKNOWN, nullable=False
    )
    original_category_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("categories.id", ondelete="SET NULL")
    )
    original_subcategory_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("subcategories.id", ondelete="SET NULL")
    )

    # ----------------------------------------------------------- corrections --
    corrected_txn_date: Mapped[date | None] = mapped_column(Date)
    corrected_description: Mapped[str | None] = mapped_column(Text)
    corrected_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    corrected_direction: Mapped[str | None] = mapped_column(String(16))
    corrected_merchant: Mapped[str | None] = mapped_column(String(255))
    corrected_payment_method: Mapped[str | None] = mapped_column(String(32))

    # ------------------------------------------------------------- effective --
    # GENERATED ALWAYS ... STORED. Read-only to the application by construction.
    txn_date: Mapped[date] = mapped_column(
        Date,
        Computed("COALESCE(corrected_txn_date, original_txn_date)", persisted=True),
    )
    description: Mapped[str] = mapped_column(
        Text,
        Computed("COALESCE(corrected_description, original_description)", persisted=True),
    )
    amount: Mapped[Decimal] = mapped_column(
        MONEY, Computed("COALESCE(corrected_amount, original_amount)", persisted=True)
    )
    direction: Mapped[str] = mapped_column(
        String(16),
        Computed("COALESCE(corrected_direction, original_direction)", persisted=True),
    )
    merchant: Mapped[str | None] = mapped_column(
        String(255),
        Computed("COALESCE(corrected_merchant, original_merchant)", persisted=True),
    )
    payment_method: Mapped[str] = mapped_column(
        String(32),
        Computed(
            "COALESCE(corrected_payment_method, original_payment_method)", persisted=True
        ),
    )

    # -------------------------------------------------------------- category --
    # Ordinary mutable columns rather than generated ones: PostgreSQL will not
    # accept a foreign key on a generated column, and referential integrity to
    # the category table is worth more here than symmetry with the fields above.
    # `original_category_id` above preserves what was first assigned.
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("categories.id", ondelete="SET NULL")
    )
    subcategory_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("subcategories.id", ondelete="SET NULL")
    )
    category_source: Mapped[str] = mapped_column(
        String(32), default=CategorySource.FALLBACK_OTHER, nullable=False
    )
    # Why this category was chosen — matched rule id, pattern, fuzzy score,
    # historical sample count, or AI model and score. Rendered as a sentence in
    # the "why was this categorised this way?" panel.
    category_reason: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    # -------------------------------------------------------------- movement --
    movement_type: Mapped[str] = mapped_column(
        String(32), default=MovementType.UNKNOWN, nullable=False
    )
    # The single flag every analytics query filters on. Transfers, card
    # payments, refunds, salary and investments are all false.
    is_expense: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    transfer_group_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("transfer_groups.id", ondelete="SET NULL")
    )

    # ------------------------------------------------------------ confidence --
    confidence_extraction: Mapped[Decimal] = mapped_column(CONFIDENCE, nullable=False)
    confidence_merchant: Mapped[Decimal] = mapped_column(CONFIDENCE, nullable=False)
    confidence_category: Mapped[Decimal] = mapped_column(CONFIDENCE, nullable=False)
    confidence_validation: Mapped[Decimal] = mapped_column(CONFIDENCE, nullable=False)

    # The gate. LEAST, not an average — see the module docstring.
    confidence_min: Mapped[Decimal] = mapped_column(
        CONFIDENCE,
        Computed(
            "LEAST(confidence_extraction, confidence_merchant, "
            "confidence_category, confidence_validation)",
            persisted=True,
        ),
    )

    # Per-field extraction scores, for the reviewer who needs to know which
    # column of the row was the doubtful one.
    field_confidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    # Mutable: it starts as a function of confidence_min but a human resolving
    # a row changes it, so it cannot be a generated column.
    review_status: Mapped[str] = mapped_column(
        String(32), default=ReviewStatus.REVIEW_REQUIRED, nullable=False
    )

    # ----------------------------------------------------------- verification --
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    verified_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ------------------------------------------------------------ provenance --
    # sha256(tenant | account | date | amount | direction | normalised text).
    # Balance is deliberately not an input — see the module docstring.
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    source_page: Mapped[int | None] = mapped_column(Integer)
    source_row: Mapped[int | None] = mapped_column(Integer)
    # The raw extracted cells, kept so a parser bug can be diagnosed years later
    # without re-reading the PDF.
    raw_row: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (
        # The database refuses duplicates. Not the application — the database.
        UniqueConstraint(
            "tenant_id", "account_id", "fingerprint",
            name="uq_transactions_tenant_id_account_id_fingerprint",
        ),
        CheckConstraint("original_amount >= 0", name="original_amount_non_negative"),
        CheckConstraint(
            "corrected_amount IS NULL OR corrected_amount >= 0",
            name="corrected_amount_non_negative",
        ),
        enum_check("original_direction", Direction),
        enum_check("corrected_direction", Direction, nullable=True),
        enum_check("original_payment_method", PaymentMethod),
        enum_check("corrected_payment_method", PaymentMethod, nullable=True),
        enum_check("category_source", CategorySource),
        enum_check("movement_type", MovementType),
        enum_check("review_status", ReviewStatus),
        confidence_check("confidence_extraction"),
        confidence_check("confidence_merchant"),
        confidence_check("confidence_category"),
        confidence_check("confidence_validation"),
        # A verified row must record who verified it and when. Without this,
        # "verified" degrades into an unaccountable boolean.
        CheckConstraint(
            "NOT is_verified OR (verified_by IS NOT NULL AND verified_at IS NOT NULL)",
            name="verified_has_actor",
        ),
        # --- indexes ---------------------------------------------------------
        # Every tenant-scoped query filters on tenant first; a leading tenant_id
        # is what makes these usable rather than decorative.
        Index("ix_transactions_tenant_date", "tenant_id", "txn_date"),
        Index("ix_transactions_tenant_expense_date", "tenant_id", "is_expense", "txn_date"),
        Index("ix_transactions_tenant_category_date", "tenant_id", "category_id", "txn_date"),
        Index("ix_transactions_tenant_account_date", "tenant_id", "account_id", "txn_date"),
        Index("ix_transactions_tenant_review", "tenant_id", "review_status"),
        Index("ix_transactions_tenant_merchant", "tenant_id", "merchant"),
        Index("ix_transactions_tenant_statement", "tenant_id", "statement_id"),
        Index("ix_transactions_transfer_group", "transfer_group_id"),
        # Supports the fuzzy near-duplicate pass, which looks for same date and
        # amount before comparing descriptions.
        Index("ix_transactions_tenant_dedup", "tenant_id", "account_id", "txn_date", "amount"),
    )


class TransactionAudit(Base, TenantScopedMixin):
    """Append-only history of every change to a transaction.

    Because ``original_*`` columns are frozen and corrections are stored
    separately, this table records the sequence of corrections rather than
    being the only defence against losing the original. Both matter: the
    columns preserve the truth, this preserves the story.
    """

    __tablename__ = "transaction_audit"

    id: Mapped[uuid.UUID] = uuid_pk()
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False
    )
    changed_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    # user | system | ai — an AI-sourced change to a verified row is rejected
    # outright by the protection trigger, so anything recorded here as `ai`
    # touched an unverified row.
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)

    field_name: Mapped[str] = mapped_column(String(64), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(String(255))

    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        Index("ix_transaction_audit_transaction", "transaction_id", "changed_at"),
        Index("ix_transaction_audit_tenant_changed", "tenant_id", "changed_at"),
    )
