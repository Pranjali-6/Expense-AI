"""FastAPI dependencies.

The important one is :func:`get_tenant_session`. Every endpoint touching tenant
data takes it, and it yields a session with ``app.current_tenant_id`` already
bound from the **access token**, never from anything the caller supplied.

That distinction is the whole authorization model. A tenant id in a path, a
query string or a body is an attacker-controlled value; taking it from the
signed token means an endpoint physically cannot be pointed at another tenant's
data, however carelessly it is written. Combined with Row Level Security, an
endpoint that forgets its WHERE clause returns the caller's rows — not
everyone's.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import (
    AuthenticationError,
    AuthorizationError,
    RateLimitError,
    ValidationFailedError,
)
from app.core.logging import bind_context, get_logger
from app.core.rate_limit import RateLimitScope, check_rate_limit
from app.core.security import decode_access_token, verify_csrf_token
from app.db.session import get_session_factory, scoped_session
from app.models.enums import UserRole
from app.services.auth import AuthenticatedUser

logger = get_logger(__name__)

REFRESH_COOKIE = "expense_refresh"
CSRF_COOKIE = "expense_csrf"
CSRF_HEADER = "X-CSRF-Token"

# auto_error=False so a missing header produces our own error shape rather than
# FastAPI's, and so optional-auth endpoints are possible.
_bearer = HTTPBearer(auto_error=False)


def client_ip(request: Request) -> str:
    """Best-effort client address.

    Trusts ``X-Forwarded-For`` because the app always sits behind nginx, which
    sets it. Only the first hop is taken — the rest is client-controlled and
    trivially spoofed.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def get_current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> AuthenticatedUser:
    """Resolve the caller from their access token.

    The token carries the tenant, so this does not need a scoped session — and
    could not have one, because the scope is what the token establishes.
    """
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Authentication is required.", error_code="missing_token")

    claims = decode_access_token(credentials.credentials)
    if claims is None:
        raise AuthenticationError(
            "Your session has expired.", error_code="invalid_token"
        )

    # The token is signed, but the account behind it may have been suspended or
    # deleted since it was issued. A 15-minute window of validity for a
    # disabled account is not acceptable for financial data.
    async with scoped_session(claims.tenant_id, actor="user") as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT id, tenant_id, email, full_name, role, auth_provider,
                           email_verified_at, created_at, last_login_at, status,
                           deleted_at
                    FROM users WHERE id = :id
                    """
                ),
                {"id": claims.user_id},
            )
        ).one_or_none()

    if row is None or row.deleted_at is not None or row.status != "active":
        raise AuthenticationError(
            "Your session is no longer valid.", error_code="account_unavailable"
        )

    bind_context(user_id=str(row.id), tenant_id=str(row.tenant_id))

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


CurrentUser = Annotated[AuthenticatedUser, Depends(get_current_user)]


async def get_tenant_session(current_user: CurrentUser) -> AsyncIterator[AsyncSession]:
    """A database session scoped to the caller's tenant.

    The scope comes from the verified token. There is deliberately no parameter
    by which a caller could influence it.
    """
    async with scoped_session(current_user.tenant_id, actor="user") as session:
        yield session


TenantSession = Annotated[AsyncSession, Depends(get_tenant_session)]


async def get_unscoped_session() -> AsyncIterator[AsyncSession]:
    """A session with **no** tenant scope.

    For authentication only, where the tenant is not yet known. Under Row Level
    Security this session can see nothing but the SECURITY DEFINER lookup
    functions, which is the intended blast radius.
    """
    async with get_session_factory()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


UnscopedSession = Annotated[AsyncSession, Depends(get_unscoped_session)]


def require_role(*roles: UserRole):
    """Dependency factory for role-gated endpoints."""
    allowed = {str(role) for role in roles}

    async def _guard(current_user: CurrentUser) -> AuthenticatedUser:
        if current_user.role not in allowed:
            logger.warning(
                "authorization_denied",
                user_id=str(current_user.id),
                tenant_id=str(current_user.tenant_id),
                error_code="insufficient_role",
            )
            raise AuthorizationError("You do not have access to this action.")
        return current_user

    return _guard


async def enforce_csrf(
    request: Request,
    csrf_header: Annotated[str | None, Header(alias=CSRF_HEADER)] = None,
) -> None:
    """Double-submit CSRF check for cookie-authenticated endpoints.

    Only refresh and logout need this: everything else authenticates with an
    ``Authorization`` header, which a cross-site page cannot cause the browser
    to attach.
    """
    if not settings.CSRF_ENABLED:
        return

    cookie_value = request.cookies.get(CSRF_COOKIE)
    if not verify_csrf_token(cookie_value, csrf_header):
        logger.warning(
            "csrf_check_failed", method=request.method, path=request.url.path,
            error_code="csrf_invalid",
        )
        raise ValidationFailedError(
            "Request could not be verified. Please refresh and try again.",
            error_code="csrf_invalid",
        )


async def rate_limit_api(request: Request, current_user: CurrentUser) -> None:
    """Per-user throttle on general API traffic."""
    result = await check_rate_limit(RateLimitScope.API, str(current_user.id))
    if not result.allowed:
        raise RateLimitError(
            "Too many requests. Please slow down.",
            details={"retry_after_seconds": result.retry_after_seconds},
        )


async def rate_limit_by_ip(request: Request, scope: RateLimitScope) -> None:
    result = await check_rate_limit(scope, client_ip(request))
    if not result.allowed:
        raise RateLimitError(
            "Too many attempts. Please try again shortly.",
            details={"retry_after_seconds": result.retry_after_seconds},
        )


def parse_uuid(value: str, field: str = "id") -> uuid.UUID:
    """Reject a malformed id before it reaches a query."""
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError):
        raise ValidationFailedError(
            "That identifier is not valid.", details={"field": field}
        ) from None
