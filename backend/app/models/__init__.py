"""Model registry.

Importing this package registers every table on ``Base.metadata``, which is
what Alembic autogenerate reflects against. A model that is not imported here
does not exist as far as migrations are concerned.

:data:`TENANT_SCOPED_TABLES` is derived from the mixin rather than hand-listed,
so a new tenant-scoped model automatically acquires a Row Level Security policy
in the next migration. Hand-maintaining that list is exactly the kind of thing
that silently goes wrong once, and then a table is readable across tenants.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from sqlalchemy import DefaultClause, text

from app.models.account import Account, IngestionSource
from app.models.ai import AIClassification, PrivacyCounter, PrivacyIncident
from app.models.base import Base, TenantScopedMixin
from app.models.category import (
    Category,
    Merchant,
    MerchantAlias,
    Subcategory,
    UserCategoryRule,
)
from app.models.intelligence import (
    Anomaly,
    Budget,
    InsightSnapshot,
    Subscription,
    TimelineEvent,
)
from app.models.job import JobEvent, ProcessingJob
from app.models.statement import Statement, StatementHealth, StatementPage
from app.models.system import AuditLog, ExtractionAccuracyRun, Notification
from app.models.tenant import RefreshToken, Tenant, User
from app.models.transaction import Transaction, TransactionAudit, TransferGroup

__all__ = [
    "Base",
    # identity
    "Tenant",
    "User",
    "RefreshToken",
    # accounts and sources
    "Account",
    "IngestionSource",
    # statements
    "Statement",
    "StatementPage",
    "StatementHealth",
    # ledger
    "Transaction",
    "TransactionAudit",
    "TransferGroup",
    # reference data
    "Category",
    "Subcategory",
    "Merchant",
    "MerchantAlias",
    "UserCategoryRule",
    # jobs
    "ProcessingJob",
    "JobEvent",
    # ai and privacy
    "AIClassification",
    "PrivacyIncident",
    "PrivacyCounter",
    # intelligence
    "Subscription",
    "Budget",
    "InsightSnapshot",
    "Anomaly",
    "TimelineEvent",
    # system
    "AuditLog",
    "Notification",
    "ExtractionAccuracyRun",
    # registries
    "TENANT_SCOPED_TABLES",
    "GLOBAL_TABLES",
]


def _mirror_python_defaults_to_server_defaults() -> None:
    """Give every defaulted NOT NULL column a matching DEFAULT in the database.

    SQLAlchemy applies a ``default=`` on the Python side, which covers ORM and
    Core inserts but not raw SQL, ``COPY``, or anything written by a psql
    session. A ``NOT NULL`` column whose default exists only in Python is
    therefore a NOT NULL violation waiting for the first bulk load — which is
    exactly how this was found: a raw INSERT that omitted
    ``users.failed_login_count``.

    Mirroring them here rather than repeating ``server_default=`` on 78 column
    definitions means the invariant holds for columns added later too, without
    anyone having to remember it. ``test_schema_defaults.py`` asserts the result.
    """
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if column.server_default is not None or column.computed is not None:
                continue
            if column.default is None or column.default.is_sequence:
                continue
            # Callables are evaluated per row (uuid4, now) and have no static
            # SQL equivalent; those columns already carry a server_default where
            # one is wanted.
            if column.default.is_callable:
                continue

            value = column.default.arg
            if isinstance(value, bool):
                literal = "true" if value else "false"
            elif isinstance(value, (int, float, Decimal)):
                literal = str(value)
            elif isinstance(value, StrEnum):
                literal = f"'{value.value}'"
            elif isinstance(value, str):
                literal = f"'{value}'"
            else:
                continue

            column.server_default = DefaultClause(text(literal))


_mirror_python_defaults_to_server_defaults()


def _tenant_scoped_tables() -> tuple[str, ...]:
    tables = {
        mapper.class_.__tablename__
        for mapper in Base.registry.mappers
        if issubclass(mapper.class_, TenantScopedMixin)
    }
    return tuple(sorted(tables))


#: Tables carrying a ``tenant_id``. Each gets an RLS policy comparing it to the
#: ``app.current_tenant_id`` session GUC.
TENANT_SCOPED_TABLES: tuple[str, ...] = _tenant_scoped_tables()

#: ``tenants`` is isolated too, but on its primary key rather than a
#: ``tenant_id`` column, so its policy is written separately.
TENANTS_TABLE = "tenants"

#: Reference data and harness output. Deliberately *not* under RLS: the merchant
#: dictionary and the category list are shared knowledge, and parser scorecards
#: describe fixtures rather than anyone's transactions. Putting these behind RLS
#: would mean every new tenant starts unable to categorise anything.
GLOBAL_TABLES: tuple[str, ...] = (
    "categories",
    "subcategories",
    "merchants",
    "merchant_aliases",
    "extraction_accuracy_runs",
)
