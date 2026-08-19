"""Tenants, users and refresh tokens."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import CITEXT, INET, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    Base,
    MediumStr,
    ShortStr,
    TenantScopedMixin,
    TimestampMixin,
    enum_check,
    uuid_pk,
)
from app.models.enums import AuthProvider, UserRole, UserStatus


class Tenant(Base, TimestampMixin):
    """The isolation boundary. Every tenant-scoped row hangs off one of these.

    A personal account gets a tenant of one; the model exists so a household or
    a small business can share a ledger later without reshaping the schema.
    """

    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[MediumStr]
    slug: Mapped[ShortStr] = mapped_column(unique=True)

    # Per-tenant AI opt-out, honoured ahead of the global AI_ENABLED setting.
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_tenants_deleted_at", "deleted_at"),)


class User(Base, TenantScopedMixin, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = uuid_pk()

    # CITEXT: case-insensitive comparison in the database, so two accounts can
    # never be created for Rahul@x.com and rahul@x.com.
    email: Mapped[str] = mapped_column(CITEXT, nullable=False)
    full_name: Mapped[MediumStr]

    # Null for OAuth-only users. Argon2id, never anything faster.
    password_hash: Mapped[str | None] = mapped_column(String(255))
    auth_provider: Mapped[str] = mapped_column(
        String(32), default=AuthProvider.PASSWORD, nullable=False
    )
    google_subject: Mapped[str | None] = mapped_column(String(255), unique=True)

    role: Mapped[str] = mapped_column(String(32), default=UserRole.OWNER, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=UserStatus.ACTIVE, nullable=False
    )

    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Throttling state for credential stuffing. Reset on a successful login.
    failed_login_count: Mapped[int] = mapped_column(default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # Email is unique globally, not per tenant: a login form has no tenant
        # context, so a shared address across tenants would make authentication
        # ambiguous before we ever reach an authorization check.
        UniqueConstraint("email", name="uq_users_email"),
        enum_check("auth_provider", AuthProvider),
        enum_check("role", UserRole),
        enum_check("status", UserStatus),
        Index("ix_users_tenant_status", "tenant_id", "status"),
    )


class RefreshToken(Base, TenantScopedMixin):
    """Rotating refresh tokens with family-based reuse detection.

    Each refresh issues a new token and revokes its predecessor. If a token that
    has already been rotated is presented again, the whole family is revoked —
    the plausible explanation is that it was stolen, and the honest response is
    to end every session derived from it.
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    # SHA-256 of the token. The raw value exists only in the client's cookie —
    # a database dump must not be a set of working credentials.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    family_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)

    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rotated_to: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))

    # Coarse client fingerprint for the sessions screen. IP is stored because a
    # user reviewing their own sessions needs it; it is never logged.
    user_agent: Mapped[str | None] = mapped_column(String(255))
    ip_address: Mapped[str | None] = mapped_column(INET)

    __table_args__ = (
        Index("ix_refresh_tokens_user_active", "user_id", "revoked_at"),
        Index("ix_refresh_tokens_family", "family_id"),
        Index("ix_refresh_tokens_expires_at", "expires_at"),
    )
