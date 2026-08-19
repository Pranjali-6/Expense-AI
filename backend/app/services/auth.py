"""Authentication: registration, login, refresh rotation, logout.

The refresh-token design is the part worth reading.

Every refresh issues a **new** token and revokes the one presented, recording
``rotated_to`` so the chain is traceable. All tokens descended from one login
share a ``family_id``. If a token that has *already been rotated* is presented
again, one of two things happened: a client raced itself, or someone stole a
token and is now using it behind the legitimate user. We cannot tell which, and
the safe reading is theft — so the entire family is revoked and everyone
re-authenticates. Losing a session is a minor annoyance; leaving a stolen token
live is not.

Rotation without reuse detection is close to pointless: a thief who copies a
token simply refreshes it forever alongside the real user.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AuthenticationError, ConflictError, ValidationFailedError
from app.core.logging import get_logger
from app.core.security import (
    account_fingerprint,  # noqa: F401  (re-exported for account creation in P3)
    create_access_token,
    generate_csrf_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    needs_rehash,
    refresh_token_expiry,
    validate_password,
    verify_password,
)
from app.db.session import scoped_session
from app.models.enums import AuditAction, AuthProvider, UserRole, UserStatus
from app.services import audit

logger = get_logger(__name__)

# After this many consecutive failures the account is locked for a cooling-off
# period. Complements the IP rate limit: that stops one source hammering many
# accounts, this stops many sources hammering one account.
MAX_FAILED_LOGINS = 8
LOCKOUT_MINUTES = 15


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    id: uuid.UUID
    tenant_id: uuid.UUID
    email: str
    full_name: str
    role: str
    auth_provider: str
    email_verified: bool
    created_at: datetime
    last_login_at: datetime | None


@dataclass(frozen=True, slots=True)
class IssuedTokens:
    access_token: str
    access_expires_at: datetime
    refresh_token: str
    refresh_expires_at: datetime
    csrf_token: str
    user: AuthenticatedUser
    #: Row id of the stored refresh token, so rotation can link the old token
    #: to its successor without a second lookup.
    refresh_token_id: uuid.UUID


# --------------------------------------------------------------------------- #
# Lookups
# --------------------------------------------------------------------------- #

async def _lookup_by_email(session: AsyncSession, email: str):
    """Resolve an email to an account across tenant boundaries.

    The one operation with no tenant context — the tenant is what is being
    looked up. It goes through a SECURITY DEFINER function returning only the
    columns a login decision needs, rather than exempting ``users`` from RLS
    for the whole application.
    """
    result = await session.execute(
        text("SELECT * FROM auth_lookup_user(CAST(:email AS citext))"),
        {"email": email},
    )
    return result.one_or_none()


async def _load_user(session: AsyncSession, user_id: uuid.UUID) -> AuthenticatedUser:
    row = (
        await session.execute(
            text(
                """
                SELECT id, tenant_id, email, full_name, role, auth_provider,
                       email_verified_at, created_at, last_login_at
                FROM users WHERE id = :id
                """
            ),
            {"id": user_id},
        )
    ).one()

    return AuthenticatedUser(
        id=row.id,
        tenant_id=row.tenant_id,
        email=row.email,
        full_name=row.full_name,
        role=row.role,
        auth_provider=row.auth_provider,
        email_verified=row.email_verified_at is not None,
        created_at=row.created_at,
        last_login_at=row.last_login_at,
    )


# --------------------------------------------------------------------------- #
# Token issuance
# --------------------------------------------------------------------------- #

async def _issue_tokens(
    session: AsyncSession,
    user: AuthenticatedUser,
    *,
    family_id: uuid.UUID | None = None,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> IssuedTokens:
    access_token, access_expires = create_access_token(
        user_id=user.id, tenant_id=user.tenant_id, role=user.role
    )

    refresh_token = generate_refresh_token()
    refresh_expires = refresh_token_expiry()
    now = datetime.now(timezone.utc)

    inserted_id = (
        await session.execute(
            text(
                """
            INSERT INTO refresh_tokens (
                tenant_id, user_id, token_hash, family_id,
                issued_at, expires_at, user_agent, ip_address
            ) VALUES (
                :tenant_id, :user_id, :token_hash, :family_id,
                :issued_at, :expires_at, :user_agent, CAST(:ip_address AS inet)
            )
            RETURNING id
            """
            ),
        {
            "tenant_id": user.tenant_id,
            "user_id": user.id,
            # Only the hash is stored. A database dump is not a set of live
            # sessions.
            "token_hash": hash_refresh_token(refresh_token),
            "family_id": family_id or uuid.uuid4(),
            "issued_at": now,
            "expires_at": refresh_expires,
            "user_agent": (user_agent or "")[:255] or None,
            "ip_address": ip_address,
            },
        )
    ).scalar_one()

    return IssuedTokens(
        access_token=access_token,
        access_expires_at=access_expires,
        refresh_token=refresh_token,
        refresh_expires_at=refresh_expires,
        csrf_token=generate_csrf_token(),
        user=user,
        refresh_token_id=inserted_id,
    )


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #

async def register(
    *,
    email: str,
    password: str,
    full_name: str,
    workspace_name: str | None = None,
    request_id: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> IssuedTokens:
    """Create a tenant and its first user.

    Registration creates the tenant too: a personal account is a tenant of one,
    and modelling it that way from the start means adding a second user later is
    a row insert rather than a migration.
    """
    problem = validate_password(password, email=email)
    if problem:
        raise ValidationFailedError(problem, error_code="weak_password")

    normalised_email = email.strip().lower()
    tenant_id = uuid.uuid4()

    # Scoped to the new tenant before anything is written — the RLS policy's
    # WITH CHECK compares the incoming row against this.
    async with scoped_session(tenant_id, actor="user") as session:
        existing = await _lookup_by_email(session, normalised_email)
        if existing is not None:
            # Deliberately the same shape of error as any other conflict, and
            # rate limited, so registration is not an account-existence oracle.
            raise ConflictError(
                "An account with that email already exists.",
                error_code="email_taken",
            )

        slug_base = normalised_email.split("@")[0][:40] or "workspace"
        await session.execute(
            text(
                "INSERT INTO tenants (id, name, slug, ai_enabled) "
                "VALUES (:id, :name, :slug, true)"
            ),
            {
                "id": tenant_id,
                "name": workspace_name or f"{full_name}'s workspace",
                "slug": f"{slug_base}-{tenant_id.hex[:8]}",
            },
        )

        user_id = uuid.uuid4()
        await session.execute(
            text(
                """
                INSERT INTO users (
                    id, tenant_id, email, full_name, password_hash,
                    auth_provider, role, status
                ) VALUES (
                    :id, :tenant_id, CAST(:email AS citext), :full_name,
                    :password_hash, :provider, :role, :status
                )
                """
            ),
            {
                "id": user_id,
                "tenant_id": tenant_id,
                "email": normalised_email,
                "full_name": full_name.strip(),
                "password_hash": hash_password(password),
                "provider": str(AuthProvider.PASSWORD),
                "role": str(UserRole.OWNER),
                "status": str(UserStatus.ACTIVE),
            },
        )

        user = await _load_user(session, user_id)
        tokens = await _issue_tokens(
            session, user, user_agent=user_agent, ip_address=ip_address
        )

        await audit.record(
            session,
            tenant_id=tenant_id,
            user_id=user_id,
            action=AuditAction.REGISTER,
            resource_type="user",
            resource_id=user_id,
            request_id=request_id,
            ip_address=ip_address,
            user_agent=user_agent,
        )

    logger.info("user_registered", tenant_id=str(tenant_id), user_id=str(user_id))
    return tokens


# --------------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------------- #

async def login(
    *,
    email: str,
    password: str,
    request_id: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> IssuedTokens:
    normalised_email = email.strip().lower()
    now = datetime.now(timezone.utc)

    # Unscoped: we do not know the tenant yet. The only thing this session can
    # reach without a scope is the SECURITY DEFINER lookup function.
    from app.db.session import get_session_factory

    async with get_session_factory()() as session:
        async with session.begin():
            record = await _lookup_by_email(session, normalised_email)

    if record is None:
        # Still perform a full Argon2 verification so an unknown address costs
        # the same time as a known one.
        verify_password(password, None)
        raise AuthenticationError("Email or password is incorrect.", error_code="invalid_credentials")

    if record.locked_until and record.locked_until > now:
        raise AuthenticationError(
            "Too many failed attempts. Try again shortly.", error_code="account_locked"
        )

    if record.status != UserStatus.ACTIVE:
        verify_password(password, None)
        raise AuthenticationError(
            "Email or password is incorrect.", error_code="invalid_credentials"
        )

    password_ok = verify_password(password, record.password_hash)

    if not password_ok:
        # Recorded in its own committed transaction, then raised.
        #
        # Raising *inside* the transaction would roll it back: the failure
        # counter would reset on every attempt, the lockout would never trigger,
        # and no failed login would ever be audited. The bookkeeping has to
        # survive the error that follows it.
        failures = record.failed_login_count + 1
        locked_until = (
            now + timedelta(minutes=LOCKOUT_MINUTES)
            if failures >= MAX_FAILED_LOGINS
            else None
        )

        async with scoped_session(record.tenant_id, actor="user") as session:
            await session.execute(
                text(
                    "UPDATE users SET failed_login_count = :n, locked_until = :locked "
                    "WHERE id = :id"
                ),
                {"n": failures, "locked": locked_until, "id": record.id},
            )
            await audit.record(
                session,
                tenant_id=record.tenant_id,
                user_id=record.id,
                action=AuditAction.LOGIN_FAILED,
                request_id=request_id,
                ip_address=ip_address,
                user_agent=user_agent,
                succeeded=False,
                details={"count": failures},
            )

        logger.warning(
            "login_failed",
            tenant_id=str(record.tenant_id),
            user_id=str(record.id),
            count=failures,
            error_code="invalid_credentials",
        )
        raise AuthenticationError(
            "Email or password is incorrect.", error_code="invalid_credentials"
        )

    async with scoped_session(record.tenant_id, actor="user") as session:
        # Cost parameters may have been raised since this hash was made; a
        # successful login is the only moment the plaintext is available to
        # upgrade it.
        updates = {"id": record.id, "now": now}
        rehash_clause = ""
        if record.password_hash and needs_rehash(record.password_hash):
            updates["password_hash"] = hash_password(password)
            rehash_clause = ", password_hash = :password_hash"

        await session.execute(
            text(
                "UPDATE users SET failed_login_count = 0, locked_until = NULL, "
                f"last_login_at = :now{rehash_clause} WHERE id = :id"
            ),
            updates,
        )

        user = await _load_user(session, record.id)
        tokens = await _issue_tokens(
            session, user, user_agent=user_agent, ip_address=ip_address
        )

        await audit.record(
            session,
            tenant_id=user.tenant_id,
            user_id=user.id,
            action=AuditAction.LOGIN,
            request_id=request_id,
            ip_address=ip_address,
            user_agent=user_agent,
        )

    logger.info("login_succeeded", tenant_id=str(user.tenant_id), user_id=str(user.id))
    return tokens


# --------------------------------------------------------------------------- #
# Refresh
# --------------------------------------------------------------------------- #

async def refresh(
    *,
    refresh_token: str,
    request_id: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> IssuedTokens:
    token_hash = hash_refresh_token(refresh_token)
    now = datetime.now(timezone.utc)

    from app.db.session import get_session_factory

    # Resolving a token to its tenant is a pre-authentication step, so it goes
    # through the narrow SECURITY DEFINER function rather than a direct read —
    # an unscoped SELECT on refresh_tokens correctly returns nothing under RLS.
    async with get_session_factory()() as session:
        async with session.begin():
            row = (
                await session.execute(
                    text("SELECT * FROM auth_lookup_refresh_token(:hash)"),
                    {"hash": token_hash},
                )
            ).one_or_none()

    if row is None:
        raise AuthenticationError("Session expired. Please sign in again.", error_code="invalid_refresh")

    # --- reuse detection ----------------------------------------------------
    # Committed in its own transaction, then raised. Revoking the family and
    # raising inside one transaction would roll the revocation back: the
    # attacker would be turned away, the legitimate session would survive, and
    # the defence would appear to work while doing nothing at all.
    if row.rotated_to is not None or row.revoked_at is not None:
        async with scoped_session(row.tenant_id, actor="user") as session:
            await session.execute(
                text(
                    "UPDATE refresh_tokens SET revoked_at = :now "
                    "WHERE family_id = :family AND revoked_at IS NULL"
                ),
                {"now": now, "family": row.family_id},
            )
            await audit.record(
                session,
                tenant_id=row.tenant_id,
                user_id=row.user_id,
                action=AuditAction.LOGOUT,
                request_id=request_id,
                ip_address=ip_address,
                user_agent=user_agent,
                succeeded=False,
                details={"reason": "refresh_token_reuse", "session_id": row.family_id},
            )

        logger.warning(
            "refresh_token_reuse_detected",
            tenant_id=str(row.tenant_id),
            user_id=str(row.user_id),
            error_code="token_reuse",
        )
        raise AuthenticationError(
            "Session ended for security reasons. Please sign in again.",
            error_code="token_reuse",
        )

    async with scoped_session(row.tenant_id, actor="user") as session:
        if row.expires_at <= now:
            raise AuthenticationError(
                "Session expired. Please sign in again.", error_code="refresh_expired"
            )

        user = await _load_user(session, row.user_id)
        tokens = await _issue_tokens(
            session,
            user,
            family_id=row.family_id,
            user_agent=user_agent,
            ip_address=ip_address,
        )

        # Link the presented token to its successor and revoke it. Setting
        # `rotated_to` is what makes a later presentation of this same token
        # detectable as reuse rather than merely expired.
        await session.execute(
            text(
                "UPDATE refresh_tokens SET revoked_at = :now, rotated_to = :new_id "
                "WHERE id = :id"
            ),
            {"now": now, "new_id": tokens.refresh_token_id, "id": row.id},
        )

    return tokens


# --------------------------------------------------------------------------- #
# Logout
# --------------------------------------------------------------------------- #

async def logout(
    *,
    refresh_token: str | None,
    request_id: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Revoke the whole family, not just the presented token.

    Signing out should end the session, and a session is the family. Revoking
    only the current token would leave its predecessors technically live.
    """
    if not refresh_token:
        return

    token_hash = hash_refresh_token(refresh_token)
    now = datetime.now(timezone.utc)

    from app.db.session import get_session_factory

    async with get_session_factory()() as session:
        async with session.begin():
            row = (
                await session.execute(
                    text("SELECT * FROM auth_lookup_refresh_token(:hash)"),
                    {"hash": token_hash},
                )
            ).one_or_none()

    if row is None:
        return

    async with scoped_session(row.tenant_id, actor="user") as session:
        await session.execute(
            text(
                "UPDATE refresh_tokens SET revoked_at = :now "
                "WHERE family_id = :family AND revoked_at IS NULL"
            ),
            {"now": now, "family": row.family_id},
        )
        await audit.record(
            session,
            tenant_id=row.tenant_id,
            user_id=row.user_id,
            action=AuditAction.LOGOUT,
            request_id=request_id,
            ip_address=ip_address,
            user_agent=user_agent,
            details={"session_id": row.family_id},
        )

    logger.info("logout", tenant_id=str(row.tenant_id), user_id=str(row.user_id))


async def revoke_all_sessions(
    session: AsyncSession, *, user_id: uuid.UUID, except_family: uuid.UUID | None = None
) -> int:
    """End every session for a user. Used on password change."""
    result = await session.execute(
        text(
            # The cast is required: asyncpg prepares statements server-side and
            # cannot infer a type for a parameter that only ever appears
            # compared against NULL.
            "UPDATE refresh_tokens SET revoked_at = now() "
            "WHERE user_id = :user_id AND revoked_at IS NULL "
            "  AND (CAST(:except_family AS uuid) IS NULL "
            "       OR family_id <> CAST(:except_family AS uuid))"
        ),
        {"user_id": user_id, "except_family": except_family},
    )
    return result.rowcount or 0
