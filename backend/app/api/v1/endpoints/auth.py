"""Authentication endpoints."""

from __future__ import annotations


from fastapi import APIRouter, Depends, Request, Response, status

from app.core.config import settings
from app.core.deps import (
    CSRF_COOKIE,
    CurrentUser,
    REFRESH_COOKIE,
    client_ip,
    enforce_csrf,
    rate_limit_by_ip,
)
from app.core.errors import AuthenticationError, NotFoundError, ValidationFailedError
from app.core.logging import get_logger
from app.core.rate_limit import RateLimitScope, reset_rate_limit
from app.core.security import hash_password, validate_password, verify_password
from app.db.session import scoped_session
from pydantic import BaseModel, ConfigDict, Field

from app.schemas.auth import (
    LoginRequest,
    MessageResponse,
    PasswordChangeRequest,
    RegisterRequest,
    SessionResponse,
    TokenResponse,
    UserResponse,
)
from app.services import audit, auth, retention
from app.models.enums import AuditAction
from sqlalchemy import text

logger = get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


def _set_auth_cookies(response: Response, tokens: auth.IssuedTokens) -> None:
    """Attach the refresh and CSRF cookies.

    The refresh cookie is httpOnly — script cannot read it, so an XSS payload
    cannot exfiltrate a long-lived credential. Its path is scoped to the auth
    endpoints, so it is not attached to ordinary API calls that have no use
    for it.

    The CSRF cookie is deliberately *not* httpOnly: the client has to read it
    to echo it back in a header. That is the whole mechanism, and it is safe
    because the value is worthless without the refresh cookie that a cross-site
    page cannot read.
    """
    max_age = settings.REFRESH_TOKEN_TTL_DAYS * 24 * 3600

    response.set_cookie(
        REFRESH_COOKIE,
        tokens.refresh_token,
        max_age=max_age,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        path="/api/v1/auth",
        domain=settings.COOKIE_DOMAIN,
    )
    response.set_cookie(
        CSRF_COOKIE,
        tokens.csrf_token,
        max_age=max_age,
        httponly=False,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        path="/",
        domain=settings.COOKIE_DOMAIN,
    )


def _clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(REFRESH_COOKIE, path="/api/v1/auth", domain=settings.COOKIE_DOMAIN)
    response.delete_cookie(CSRF_COOKIE, path="/", domain=settings.COOKIE_DOMAIN)


def _token_response(tokens: auth.IssuedTokens) -> TokenResponse:
    return TokenResponse(
        access_token=tokens.access_token,
        expires_in=settings.ACCESS_TOKEN_TTL_MINUTES * 60,
        user=UserResponse(
            id=tokens.user.id,
            email=tokens.user.email,
            full_name=tokens.user.full_name,
            role=tokens.user.role,
            tenant_id=tokens.user.tenant_id,
            auth_provider=tokens.user.auth_provider,
            email_verified=tokens.user.email_verified,
            created_at=tokens.user.created_at,
            last_login_at=tokens.user.last_login_at,
        ),
    )


@router.post(
    "/register",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account and its workspace",
)
async def register(
    payload: RegisterRequest, request: Request, response: Response
) -> TokenResponse:
    await rate_limit_by_ip(request, RateLimitScope.REGISTER)

    tokens = await auth.register(
        email=payload.email,
        password=payload.password,
        full_name=payload.full_name,
        workspace_name=payload.workspace_name,
        request_id=getattr(request.state, "request_id", None),
        ip_address=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    _set_auth_cookies(response, tokens)
    return _token_response(tokens)


@router.post("/login", response_model=TokenResponse, summary="Sign in")
async def login(
    payload: LoginRequest, request: Request, response: Response
) -> TokenResponse:
    await rate_limit_by_ip(request, RateLimitScope.LOGIN)

    tokens = await auth.login(
        email=payload.email,
        password=payload.password,
        request_id=getattr(request.state, "request_id", None),
        ip_address=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )

    # Proving who you are clears the throttle, so a run of typos does not keep
    # someone locked out once they get it right.
    await reset_rate_limit(RateLimitScope.LOGIN, client_ip(request))

    _set_auth_cookies(response, tokens)
    return _token_response(tokens)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    dependencies=[Depends(enforce_csrf)],
    summary="Exchange the refresh cookie for a new access token",
)
async def refresh(request: Request, response: Response) -> TokenResponse:
    await rate_limit_by_ip(request, RateLimitScope.REFRESH)

    refresh_token = request.cookies.get(REFRESH_COOKIE)
    if not refresh_token:
        raise AuthenticationError("No active session.", error_code="missing_refresh")

    try:
        tokens = await auth.refresh(
            refresh_token=refresh_token,
            request_id=getattr(request.state, "request_id", None),
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
    except AuthenticationError:
        # Clear the cookies on any refresh failure. Leaving a dead cookie in
        # place makes the client retry forever against a session that is gone.
        _clear_auth_cookies(response)
        raise

    _set_auth_cookies(response, tokens)
    return _token_response(tokens)


@router.post(
    "/logout",
    response_model=MessageResponse,
    dependencies=[Depends(enforce_csrf)],
    summary="Sign out and revoke the session",
)
async def logout(request: Request, response: Response) -> MessageResponse:
    await auth.logout(
        refresh_token=request.cookies.get(REFRESH_COOKIE),
        request_id=getattr(request.state, "request_id", None),
        ip_address=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    _clear_auth_cookies(response)
    return MessageResponse(message="Signed out.")


@router.get("/me", response_model=UserResponse, summary="The signed-in user")
async def me(current_user: CurrentUser) -> UserResponse:
    return UserResponse(
        id=current_user.id,
        email=current_user.email,
        full_name=current_user.full_name,
        role=current_user.role,
        tenant_id=current_user.tenant_id,
        auth_provider=current_user.auth_provider,
        email_verified=current_user.email_verified,
        created_at=current_user.created_at,
        last_login_at=current_user.last_login_at,
    )


@router.get(
    "/sessions",
    response_model=list[SessionResponse],
    summary="Active sessions on this account",
)
async def list_sessions(request: Request, current_user: CurrentUser) -> list[SessionResponse]:
    from app.core.security import hash_refresh_token

    current_token = request.cookies.get(REFRESH_COOKIE)
    current_hash = hash_refresh_token(current_token) if current_token else None

    async with scoped_session(current_user.tenant_id) as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT DISTINCT ON (family_id)
                           id, family_id, issued_at, expires_at, user_agent, token_hash
                    FROM refresh_tokens
                    WHERE user_id = :user_id
                      AND revoked_at IS NULL
                      AND expires_at > now()
                    ORDER BY family_id, issued_at DESC
                    """
                ),
                {"user_id": current_user.id},
            )
        ).all()

    return [
        SessionResponse(
            id=row.family_id,
            issued_at=row.issued_at,
            expires_at=row.expires_at,
            user_agent=row.user_agent,
            current=current_hash is not None and row.token_hash == current_hash,
        )
        for row in rows
    ]


@router.delete(
    "/sessions/{family_id}",
    response_model=MessageResponse,
    summary="Revoke one session",
)
async def revoke_session(
    family_id: str, request: Request, current_user: CurrentUser
) -> MessageResponse:
    from app.core.deps import parse_uuid

    target = parse_uuid(family_id, "family_id")

    async with scoped_session(current_user.tenant_id) as session:
        result = await session.execute(
            text(
                "UPDATE refresh_tokens SET revoked_at = now() "
                "WHERE family_id = :family AND user_id = :user_id "
                "  AND revoked_at IS NULL"
            ),
            {"family": target, "user_id": current_user.id},
        )
        if not result.rowcount:
            # RLS already prevents reaching another tenant's rows; the
            # user_id filter prevents reaching another user within the same
            # tenant. Either way the answer is "no such session".
            raise NotFoundError("That session does not exist.")

        await audit.record(
            session,
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
            action=AuditAction.LOGOUT,
            request_id=getattr(request.state, "request_id", None),
            ip_address=client_ip(request),
            details={"session_id": target},
        )

    return MessageResponse(message="Session revoked.")


@router.post(
    "/password",
    response_model=MessageResponse,
    summary="Change password",
)
async def change_password(
    payload: PasswordChangeRequest, request: Request, current_user: CurrentUser
) -> MessageResponse:
    problem = validate_password(payload.new_password, email=current_user.email)
    if problem:
        raise ValidationFailedError(problem, error_code="weak_password")

    async with scoped_session(current_user.tenant_id) as session:
        stored = (
            await session.execute(
                text("SELECT password_hash FROM users WHERE id = :id"),
                {"id": current_user.id},
            )
        ).scalar_one_or_none()

        if not verify_password(payload.current_password, stored):
            raise AuthenticationError(
                "Your current password is incorrect.", error_code="invalid_credentials"
            )

        await session.execute(
            text("UPDATE users SET password_hash = :hash WHERE id = :id"),
            {"hash": hash_password(payload.new_password), "id": current_user.id},
        )

        # Every other session ends. A password change is usually a response to
        # a suspected compromise, and leaving other sessions live would defeat
        # the point of changing it.
        revoked = await auth.revoke_all_sessions(session, user_id=current_user.id)

        await audit.record(
            session,
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
            action=AuditAction.PASSWORD_CHANGE,
            request_id=getattr(request.state, "request_id", None),
            ip_address=client_ip(request),
            details={"count": revoked},
        )

    return MessageResponse(
        message="Password changed. Other sessions have been signed out."
    )


# --------------------------------------------------------------------------- #
# Google OAuth
#
# Implemented but disabled by default: it needs credentials only the operator
# has. When GOOGLE_OAUTH_ENABLED is false these endpoints report 404 rather
# than 500, so a probe cannot tell a misconfiguration from a deliberate choice.
# --------------------------------------------------------------------------- #

class AccountDeletionRequest(BaseModel):
    """Deleting an account needs proof it is you, and an explicit intent.

    Two independent confirmations, because this is the one action in the
    product with no undo. Re-entering the password proves the session was not
    left open on a shared machine; typing the confirmation phrase proves the
    button was not hit by accident. Neither alone is enough.
    """

    model_config = ConfigDict(extra="forbid")

    password: str | None = Field(default=None, max_length=256)
    confirm: str = Field(description='Must be exactly "DELETE MY DATA".')


DELETION_PHRASE = "DELETE MY DATA"


@router.delete(
    "/account",
    response_model=MessageResponse,
    summary="Erase this account and every trace of its data",
)
async def delete_account(
    payload: AccountDeletionRequest,
    request: Request,
    response: Response,
    current_user: CurrentUser,
) -> MessageResponse:
    """Irreversible. Removes stored PDFs, then every row, including the audit
    trail that recorded the request.

    Objects go before rows, and the order is the whole design: the storage keys
    live *in* the rows, so deleting rows first would leave financial documents
    in a bucket with nothing left that knows they exist. The other way round,
    an interruption leaves a row pointing at a missing object — visible,
    recoverable, and obviously wrong rather than invisibly wrong.

    Done inline rather than queued. A user who asks to be forgotten should get
    an answer that means it, not a 202 and a promise; and a background job that
    fails leaves an account that looks deleted and is not.
    """
    if payload.confirm != DELETION_PHRASE:
        raise ValidationFailedError(
            f'Type "{DELETION_PHRASE}" to confirm.', error_code="confirmation_required"
        )

    # A password account must re-authenticate. An OAuth account has no password
    # to check — the confirmation phrase and a live Google session are what it
    # has, and inventing a password requirement it cannot satisfy would just
    # make erasure impossible for those users.
    if current_user.auth_provider == "password":
        async with scoped_session(current_user.tenant_id) as session:
            row = (
                await session.execute(
                    text("SELECT password_hash FROM users WHERE id = :id"),
                    {"id": current_user.id},
                )
            ).one_or_none()
        if row is None or not verify_password(payload.password or "", row.password_hash):
            raise AuthenticationError(
                "That password is not correct.", error_code="invalid_password"
            )

    from app.services.storage import delete_statement

    async with scoped_session(current_user.tenant_id, actor="system") as session:
        keys = await retention.storage_keys(session)
    for key in keys:
        delete_statement(storage_key=key)

    async with scoped_session(current_user.tenant_id, actor="system") as session:
        counts = await retention.erase_tenant(session, tenant_id=current_user.tenant_id)

    # Logged with the tenant id and counts only. There is no audit row to write:
    # the audit table went with everything else, which is what erasure means.
    logger.info(
        "account_erased",
        tenant_id=str(current_user.tenant_id),
        count=counts.get("transactions", 0),
        status="ok",
    )

    # The injected response, not a new one: headers set on a locally
    # constructed Response are discarded when a model is returned instead.
    _clear_auth_cookies(response)
    return MessageResponse(
        message="Your account and all of its data have been permanently deleted."
    )


@router.get("/oauth/google", summary="Begin Google sign-in")
async def google_start(request: Request):
    if not settings.GOOGLE_OAUTH_ENABLED:
        raise NotFoundError("Google sign-in is not enabled.")
    from app.services.oauth import build_authorization_url

    return await build_authorization_url(request)


@router.get("/oauth/google/callback", summary="Complete Google sign-in")
async def google_callback(request: Request, response: Response):
    if not settings.GOOGLE_OAUTH_ENABLED:
        raise NotFoundError("Google sign-in is not enabled.")
    from app.services.oauth import complete_authorization

    tokens = await complete_authorization(request)
    _set_auth_cookies(response, tokens)
    return _token_response(tokens)
