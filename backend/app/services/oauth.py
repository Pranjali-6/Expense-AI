"""Google OAuth sign-in.

Disabled by default — it needs a client id and secret that only the operator
has. The code is complete so enabling it is a configuration change rather than
a development task.

Two decisions worth noting:

*   **Accounts are linked by Google's stable subject id, not by email.** Email
    addresses at Google can change; ``sub`` cannot. Matching on email would also
    let anyone who acquires an address take over the account it once belonged to.
*   **An unverified Google email is rejected.** Google will happily assert an
    address it has not verified, and accepting one would let an attacker
    register a Google account claiming someone else's address and inherit their
    workspace.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from authlib.integrations.starlette_client import OAuth
from fastapi import Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text

from app.core.config import settings
from app.core.errors import AuthenticationError, ConflictError
from app.core.logging import get_logger
from app.db.session import get_session_factory, scoped_session
from app.models.enums import AuditAction, AuthProvider, UserRole, UserStatus
from app.services import audit
from app.services.auth import AuthenticatedUser, IssuedTokens, _issue_tokens, _load_user

logger = get_logger(__name__)

_oauth: OAuth | None = None


def _client() -> OAuth:
    global _oauth
    if _oauth is None:
        oauth = OAuth()
        oauth.register(
            name="google",
            client_id=settings.GOOGLE_CLIENT_ID,
            client_secret=settings.GOOGLE_CLIENT_SECRET,
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )
        _oauth = oauth
    return _oauth


async def build_authorization_url(request: Request) -> RedirectResponse:
    google = _client().create_client("google")
    return await google.authorize_redirect(request, settings.GOOGLE_REDIRECT_URI)


async def complete_authorization(request: Request) -> IssuedTokens:
    google = _client().create_client("google")

    try:
        token = await google.authorize_access_token(request)
    except Exception:
        # The provider's error text can contain the authorization code.
        logger.warning("oauth_exchange_failed", provider="google", error_code="exchange_failed")
        raise AuthenticationError(
            "Google sign-in could not be completed.", error_code="oauth_failed"
        ) from None

    claims = token.get("userinfo") or {}
    subject = claims.get("sub")
    email = (claims.get("email") or "").strip().lower()
    email_verified = bool(claims.get("email_verified"))
    full_name = claims.get("name") or email.split("@")[0]

    if not subject or not email:
        raise AuthenticationError(
            "Google did not return enough information to sign in.",
            error_code="oauth_incomplete",
        )

    if not email_verified:
        raise AuthenticationError(
            "Your Google email address is not verified.", error_code="oauth_unverified"
        )

    factory = get_session_factory()

    # Match on the stable subject first.
    async with factory() as session:
        async with session.begin():
            existing = (
                await session.execute(
                    text("SELECT * FROM auth_lookup_user_by_google(:subject)"),
                    {"subject": subject},
                )
            ).one_or_none()

            by_email = None
            if existing is None:
                by_email = (
                    await session.execute(
                        text("SELECT * FROM auth_lookup_user(CAST(:email AS citext))"),
                        {"email": email},
                    )
                ).one_or_none()

    if existing is not None:
        if existing.status != UserStatus.ACTIVE:
            raise AuthenticationError(
                "This account is not available.", error_code="account_unavailable"
            )
        tenant_id, user_id = existing.tenant_id, existing.id

    elif by_email is not None:
        # An account exists with this address but was created with a password.
        # Silently linking would let anyone who can obtain a Google account for
        # that address sign in without ever knowing the password.
        raise ConflictError(
            "An account with that email already exists. Sign in with your "
            "password, then connect Google from Settings.",
            error_code="email_taken",
        )

    else:
        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()
        async with scoped_session(tenant_id, actor="user") as session:
            await session.execute(
                text(
                    "INSERT INTO tenants (id, name, slug, ai_enabled) "
                    "VALUES (:id, :name, :slug, true)"
                ),
                {
                    "id": tenant_id,
                    "name": f"{full_name}'s workspace",
                    "slug": f"{email.split('@')[0][:40]}-{tenant_id.hex[:8]}",
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO users (
                        id, tenant_id, email, full_name, password_hash,
                        auth_provider, google_subject, role, status,
                        email_verified_at
                    ) VALUES (
                        :id, :tenant_id, CAST(:email AS citext), :full_name, NULL,
                        :provider, :subject, :role, :status, :verified_at
                    )
                    """
                ),
                {
                    "id": user_id,
                    "tenant_id": tenant_id,
                    "email": email,
                    "full_name": full_name,
                    "provider": str(AuthProvider.GOOGLE),
                    "subject": subject,
                    "role": str(UserRole.OWNER),
                    "status": str(UserStatus.ACTIVE),
                    "verified_at": datetime.now(timezone.utc),
                },
            )

    async with scoped_session(tenant_id, actor="user") as session:
        await session.execute(
            text("UPDATE users SET last_login_at = now() WHERE id = :id"),
            {"id": user_id},
        )
        user: AuthenticatedUser = await _load_user(session, user_id)
        tokens = await _issue_tokens(
            session,
            user,
            user_agent=request.headers.get("user-agent"),
            ip_address=request.client.host if request.client else None,
        )
        await audit.record(
            session,
            tenant_id=tenant_id,
            user_id=user_id,
            action=AuditAction.LOGIN,
            request_id=getattr(request.state, "request_id", None),
            details={"provider": "google"},
        )

    logger.info("oauth_login", provider="google", tenant_id=str(tenant_id), user_id=str(user_id))
    return tokens
