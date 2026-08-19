"""Bank accounts and credit cards."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    Base,
    MediumStr,
    TenantScopedMixin,
    TimestampMixin,
    enum_check,
    uuid_pk,
)
from app.models.enums import AccountStatus, AccountType, IngestionSourceType


class Account(Base, TenantScopedMixin, TimestampMixin):
    """An account discovered from statements. Users never type one in.

    **No full account number is stored anywhere in this table.** Statements are
    matched to accounts by the last four digits plus bank plus account type,
    which is sufficient to disambiguate a personal set of accounts and leaves
    nothing worth stealing. ``account_fingerprint`` is a salted hash used for
    matching when even four digits would be ambiguous.
    """

    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = uuid_pk()

    # Free-form rather than an enum: the parser registry is pluggable, and a
    # new bank should not require a schema migration.
    bank_code: Mapped[str] = mapped_column(String(32), nullable=False)
    bank_name: Mapped[MediumStr]

    account_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=AccountStatus.ACTIVE, nullable=False
    )

    # The only account-number fragment that exists in this system.
    account_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    account_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    display_name: Mapped[str | None] = mapped_column(String(120))

    # Latest balance seen on an imported statement — extracted, not computed,
    # so it is only as current as the most recent import.
    current_balance: Mapped[Decimal | None] = mapped_column()
    balance_as_of: Mapped[date | None] = mapped_column(Date)

    credit_limit: Mapped[Decimal | None] = mapped_column()
    statement_day: Mapped[int | None] = mapped_column()

    # Statement coverage, for showing gaps in the Accounts screen.
    coverage_start: Mapped[date | None] = mapped_column(Date)
    coverage_end: Mapped[date | None] = mapped_column(Date)
    last_imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "account_fingerprint",
            name="uq_accounts_tenant_id_account_fingerprint",
        ),
        enum_check("account_type", AccountType),
        enum_check("status", AccountStatus),
        Index("ix_accounts_tenant_status", "tenant_id", "status"),
        Index("ix_accounts_tenant_bank", "tenant_id", "bank_code"),
    )


class IngestionSource(Base, TenantScopedMixin, TimestampMixin):
    """Where a batch of transactions came from.

    Only ``pdf_upload`` is implemented. CSV, direct bank APIs and Account
    Aggregator are reserved: the canonical transaction schema is the same for
    all of them, so adding one is a new source implementation rather than a
    reshaping of the ledger.
    """

    __tablename__ = "ingestion_sources"

    id: Mapped[uuid.UUID] = uuid_pk()
    source_type: Mapped[str] = mapped_column(
        String(32), default=IngestionSourceType.PDF_UPLOAD, nullable=False
    )
    label: Mapped[MediumStr]
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)

    __table_args__ = (
        enum_check("source_type", IngestionSourceType),
        Index("ix_ingestion_sources_tenant_type", "tenant_id", "source_type"),
    )
