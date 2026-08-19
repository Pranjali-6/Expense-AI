"""Declarative base, shared column types and mixins."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    MetaData,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Predictable constraint names, so migrations can reference them and a failing
# constraint tells you which one it was.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map = {
        dict[str, Any]: JSONB,
        list[str]: JSONB,
        Decimal: Numeric(18, 2),
        datetime: DateTime(timezone=True),
        date: Date,
        uuid.UUID: PgUUID(as_uuid=True),
    }

    def __repr__(self) -> str:
        # Deliberately id-only. A default repr would print every column, and
        # this object graph carries amounts, merchants and descriptions — the
        # exact things that must never reach a log or a traceback.
        return f"<{type(self).__name__} id={getattr(self, 'id', None)}>"


# --------------------------------------------------------------------------- #
# Column type aliases
# --------------------------------------------------------------------------- #

#: Money. NUMERIC(18, 2) everywhere, Decimal in Python, string over JSON.
#: 18 digits holds ₹9,999,999,999,999,999.99 — far past any personal ledger,
#: and cheap enough that there is no reason to be clever.
#:
#: Applied automatically to every ``Mapped[Decimal]`` via `type_annotation_map`,
#: so a money column cannot accidentally be declared as a float.
MONEY = Numeric(18, 2)

#: A confidence score in [0, 1]. Three decimal places is exactly the resolution
#: the 0.97 / 0.90 gates need, and no more.
CONFIDENCE = Numeric(4, 3)

ShortStr = Annotated[str, mapped_column(String(64))]
MediumStr = Annotated[str, mapped_column(String(255))]
LongText = Annotated[str, mapped_column(Text)]


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )


def confidence_check(column: str) -> CheckConstraint:
    return CheckConstraint(
        f"{column} >= 0 AND {column} <= 1", name=f"{column}_in_range"
    )


def enum_check(column: str, values: type[Any], *, nullable: bool = False) -> CheckConstraint:
    """CHECK constraint standing in for a native PostgreSQL enum.

    See ``app.models.enums`` for why VARCHAR + CHECK rather than a native type.
    """
    allowed = ", ".join(f"'{member.value}'" for member in values)
    predicate = f"{column} IN ({allowed})"
    if nullable:
        predicate = f"{column} IS NULL OR {predicate}"
    return CheckConstraint(predicate, name=f"{column}_valid")


# --------------------------------------------------------------------------- #
# Mixins
# --------------------------------------------------------------------------- #

class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        # Maintained by the set_updated_at trigger rather than by the ORM, so
        # raw SQL and background jobs cannot leave it stale.
    )


class TenantScopedMixin:
    """Marks a table as subject to Row Level Security.

    Adding this mixin is not enough on its own — the policy is created in the
    RLS migration, which reads ``TENANT_SCOPED_TABLES`` so the two can never
    drift apart. A table with a ``tenant_id`` and no policy would be invisible
    to a reader and wide open to a query that forgot its filter.
    """

    @property
    def __tenant_scoped__(self) -> bool:
        return True

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )


def tenant_index(table: str, *columns: str, unique: bool = False) -> Index:
    """Composite index led by ``tenant_id``.

    Every tenant-scoped query filters on tenant first, so a leading tenant_id
    is what makes these indexes usable rather than decorative.
    """
    name = f"ix_{table}_tenant_{'_'.join(columns)}"
    return Index(name, "tenant_id", *columns, unique=unique)
