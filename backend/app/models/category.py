"""Categories, the merchant dictionary, and user rules.

Categories, subcategories, merchants and merchant aliases are **global** —
deliberately not tenant-scoped, and therefore not under Row Level Security.
They are reference data: the fact that "SWIGGYINSTAMART" means Swiggy is not
anyone's private information, and duplicating the dictionary per tenant would
mean every tenant starts from zero knowledge.

``user_category_rules`` is the tenant-scoped counterpart: what *this* user has
decided a merchant means to them. It outranks everything else in the cascade.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    CONFIDENCE,
    MONEY,
    Base,
    MediumStr,
    ShortStr,
    TenantScopedMixin,
    TimestampMixin,
    confidence_check,
    uuid_pk,
)


class Category(Base, TimestampMixin):
    """One of the 22 canonical categories. Global reference data."""

    __tablename__ = "categories"

    id: Mapped[uuid.UUID] = uuid_pk()
    slug: Mapped[ShortStr] = mapped_column(unique=True)
    name: Mapped[MediumStr]

    # Whether spend in this category counts towards "expenses". Transfers,
    # credit-card payments, refunds, salary and investments do not — this is
    # the column that stops the dashboard double-counting.
    is_expense: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_income: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_system: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Presentation, kept with the data so every surface agrees.
    icon: Mapped[str | None] = mapped_column(String(48))
    color: Mapped[str | None] = mapped_column(String(16))
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (Index("ix_categories_sort_order", "sort_order"),)


class Subcategory(Base, TimestampMixin):
    __tablename__ = "subcategories"

    id: Mapped[uuid.UUID] = uuid_pk()
    category_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("categories.id", ondelete="CASCADE"), nullable=False
    )
    slug: Mapped[ShortStr]
    name: Mapped[MediumStr]
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        UniqueConstraint("category_id", "slug", name="uq_subcategories_category_id_slug"),
        Index("ix_subcategories_category", "category_id"),
    )


class Merchant(Base, TimestampMixin):
    """A normalised merchant and its default category. Global dictionary."""

    __tablename__ = "merchants"

    id: Mapped[uuid.UUID] = uuid_pk()
    slug: Mapped[ShortStr] = mapped_column(unique=True)
    display_name: Mapped[MediumStr]

    category_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("categories.id", ondelete="SET NULL")
    )
    subcategory_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("subcategories.id", ondelete="SET NULL")
    )

    # `verified` mappings are curated and sit high in the cascade; unverified
    # ones are observations and rank below deterministic rules.
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Merchant Category Code, when we know it. A hint the privacy gateway is
    # allowed to forward, because it describes a business type, not a person.
    mcc: Mapped[str | None] = mapped_column(String(8))

    website: Mapped[str | None] = mapped_column(String(255))
    is_subscription_like: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    __table_args__ = (
        Index("ix_merchants_category", "category_id"),
        Index("ix_merchants_verified", "is_verified"),
    )


class MerchantAlias(Base, TimestampMixin):
    """A raw statement fragment that resolves to a merchant.

    This is what turns ``UPI-SWIGGY@YBL-8829172``, ``SWIGGYINSTAMART`` and
    ``SWIGGY*ORDER`` into one merchant. Patterns are matched after the
    description has been stripped of reference numbers and UPI handles.
    """

    __tablename__ = "merchant_aliases"

    id: Mapped[uuid.UUID] = uuid_pk()
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False
    )

    pattern: Mapped[str] = mapped_column(String(255), nullable=False)
    # `exact` | `prefix` | `contains` | `regex` — evaluated most specific first,
    # so a precise alias always beats a loose substring.
    match_type: Mapped[str] = mapped_column(String(16), default="contains", nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "merchant_id", "pattern", "match_type",
            name="uq_merchant_aliases_merchant_id_pattern_match_type",
        ),
        Index("ix_merchant_aliases_pattern", "pattern"),
        Index("ix_merchant_aliases_priority", "priority"),
    )


class UserCategoryRule(Base, TenantScopedMixin, TimestampMixin):
    """A rule created when a user corrects a category.

    This is how the system learns. Correcting one Swiggy transaction to Food
    writes a rule here, and every future Swiggy transaction is categorised as
    Food by the top tier of the cascade — never by the AI, and never
    overwritten by it.
    """

    __tablename__ = "user_category_rules"

    id: Mapped[uuid.UUID] = uuid_pk()
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    # Matched against the normalised merchant, not the raw description — so the
    # rule survives the bank changing its reference-number format.
    merchant_pattern: Mapped[str] = mapped_column(String(255), nullable=False)
    match_type: Mapped[str] = mapped_column(String(16), default="exact", nullable=False)

    category_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("categories.id", ondelete="CASCADE"), nullable=False
    )
    subcategory_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("subcategories.id", ondelete="SET NULL")
    )

    # Optional narrowing, for rules like "Amazon over ₹5,000 is Shopping".
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE")
    )
    # NUMERIC, not float. These are rupee thresholds compared against
    # transaction amounts, and a float boundary would make a rule match or miss
    # unpredictably at its own edge.
    min_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    max_amount: Mapped[Decimal | None] = mapped_column(MONEY)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)

    # Surfaced in the explainability panel: "applied 47 times".
    times_applied: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    confidence: Mapped[Decimal] = mapped_column(
        CONFIDENCE, default=Decimal("1.000"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "merchant_pattern", "match_type", "account_id",
            name="uq_user_category_rules_tenant_pattern",
        ),
        confidence_check("confidence"),
        Index("ix_user_category_rules_tenant_active", "tenant_id", "is_active"),
        Index("ix_user_category_rules_tenant_pattern", "tenant_id", "merchant_pattern"),
    )
