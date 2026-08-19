"""Cryptographic primitives: password hashing, JWTs, opaque tokens, CSRF.

Two shapes of credential, chosen for different jobs:

*   **Access token — a short-lived JWT.** Stateless, so every request can be
    authorised without a database round trip. Fifteen minutes, held in memory
    by the client and never written to storage a script can read.
*   **Refresh token — a long-lived opaque random string.** Stateful, because a
    long-lived credential must be revocable. Only its SHA-256 hash is stored,
    so a database dump is not a set of working sessions.

A JWT cannot be revoked before it expires; an opaque token cannot be validated
without a lookup. Using each where its weakness does not matter is the point.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Final

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from argon2.low_level import Type

from app.core.config import settings

# OWASP's minimum for Argon2id (19 MiB, 2 iterations, 1 lane). time_cost is
# raised to 3: a login is not latency-critical, and the extra pass costs us
# milliseconds while costing an attacker a third more of everything.
_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=19 * 1024,
    parallelism=1,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)

#: Verified against when no user matches, so a login attempt for an unknown
#: address costs the same time as one for a real account. Without this, response
#: latency alone enumerates which emails are registered.
_DUMMY_HASH: Final = _hasher.hash("expense-ai-timing-equaliser")

JWT_ISSUER: Final = "expense-ai"
JWT_AUDIENCE: Final = "expense-ai-api"


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #

def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    """Verify a password, in constant-ish time whether or not the user exists.

    Passing ``None`` still performs a full Argon2 verification against a dummy
    hash and returns False.
    """
    if password_hash is None:
        # Burn the same work rather than returning early, so "no such user"
        # and "wrong password" are indistinguishable by timing.
        try:
            _hasher.verify(_DUMMY_HASH, password)
        except Exception:
            pass
        return False

    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False
    except Exception:
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


@dataclass(frozen=True, slots=True)
class PasswordPolicy:
    """Length over composition rules.

    NIST dropped mandatory character-class rules years ago: they push people
    towards `Password1!` and no further. Twelve characters with a check against
    the obvious choices is a better filter than four character classes.
    """

    min_length: int = 12
    max_length: int = 256


_COMMON_PASSWORDS: Final = frozenset({
    "password", "password1", "password123", "123456789", "qwertyuiop",
    "letmein123", "welcome123", "admin123456", "iloveyou123", "changeme123",
    "expenseai123", "abcd1234567", "1234567890ab",
})


def validate_password(password: str, *, email: str | None = None) -> str | None:
    """Return a human-readable problem, or None if the password is acceptable."""
    policy = PasswordPolicy()

    if len(password) < policy.min_length:
        return f"Password must be at least {policy.min_length} characters."
    if len(password) > policy.max_length:
        return f"Password must be at most {policy.max_length} characters."
    if password.lower() in _COMMON_PASSWORDS:
        return "That password is too common. Please choose another."
    if email and email.split("@")[0].lower() in password.lower():
        return "Password must not contain your email address."
    if len(set(password)) < 5:
        return "Password must not repeat a handful of characters."
    return None


# --------------------------------------------------------------------------- #
# Access tokens (JWT)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    role: str
    jti: str
    expires_at: datetime


def create_access_token(
    *, user_id: uuid.UUID, tenant_id: uuid.UUID, role: str
) -> tuple[str, datetime]:
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=settings.ACCESS_TOKEN_TTL_MINUTES)

    payload: dict[str, Any] = {
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "sub": str(user_id),
        # The tenant travels in the token so the RLS scope can be established
        # before the first query, rather than being looked up by a query that
        # would itself need a scope.
        "tid": str(tenant_id),
        "role": role,
        "typ": "access",
        "jti": secrets.token_urlsafe(16),
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return token, expires_at


def decode_access_token(token: str) -> AccessTokenClaims | None:
    """Decode and fully validate an access token. Returns None if unusable."""
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            # Pinned explicitly. Accepting the token's own `alg` is how the
            # `alg: none` and RS256→HS256 confusion attacks work.
            algorithms=[settings.JWT_ALGORITHM],
            audience=JWT_AUDIENCE,
            issuer=JWT_ISSUER,
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
    except jwt.PyJWTError:
        return None

    if payload.get("typ") != "access":
        return None

    try:
        return AccessTokenClaims(
            user_id=uuid.UUID(payload["sub"]),
            tenant_id=uuid.UUID(payload["tid"]),
            role=str(payload["role"]),
            jti=str(payload.get("jti", "")),
            expires_at=datetime.fromtimestamp(payload["exp"], tz=timezone.utc),
        )
    except (KeyError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Refresh tokens (opaque)
# --------------------------------------------------------------------------- #

def generate_refresh_token() -> str:
    """256 bits of randomness. Never derived from anything guessable."""
    return secrets.token_urlsafe(32)


def hash_refresh_token(token: str) -> str:
    """SHA-256, not Argon2.

    Deliberate: the token is already 256 bits of uniform randomness, so there
    is no dictionary to slow an attacker down with, and a refresh happens on
    every page load — spending 19 MiB and three passes on each one would be a
    self-inflicted denial of service.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def refresh_token_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_TTL_DAYS)


# --------------------------------------------------------------------------- #
# CSRF
# --------------------------------------------------------------------------- #

def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def verify_csrf_token(cookie_value: str | None, header_value: str | None) -> bool:
    """Double-submit check.

    The refresh and logout endpoints authenticate with a cookie, so they are the
    only ones a cross-site form could trigger. The CSRF cookie is readable by
    same-origin JavaScript and echoed in a header; a cross-origin page can cause
    the cookie to be *sent* but cannot read it to set the header.

    SameSite=Lax already blocks most of this. Both are cheap; neither is
    sufficient alone against every browser and every redirect path.
    """
    if not cookie_value or not header_value:
        return False
    return hmac.compare_digest(cookie_value, header_value)


# --------------------------------------------------------------------------- #
# Misc
# --------------------------------------------------------------------------- #

def account_fingerprint(tenant_id: uuid.UUID, bank_code: str, account_type: str, last4: str) -> str:
    """Stable per-tenant identifier for an account, from non-secret parts.

    Keyed with the master KEK so the value is useless outside this deployment
    and cannot be recomputed from a leaked row.
    """
    message = f"{tenant_id}:{bank_code}:{account_type}:{last4}".encode("utf-8")
    return hmac.new(
        settings.STORAGE_MASTER_KEK.encode("utf-8"), message, hashlib.sha256
    ).hexdigest()
