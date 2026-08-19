"""Authentication request and response schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str = Field(min_length=12, max_length=256)
    full_name: str = Field(min_length=1, max_length=255)
    # Optional; defaults to the user's own name. Present so a household or a
    # small business can be named at sign-up without a second step.
    workspace_name: str | None = Field(default=None, max_length=255)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str
    role: str
    tenant_id: uuid.UUID
    auth_provider: str
    email_verified: bool
    created_at: datetime
    last_login_at: datetime | None


class TokenResponse(BaseModel):
    """The access token travels in the body, never in a cookie.

    A cookie would be sent automatically on every request, including
    cross-origin ones a page did not intend — which is the CSRF surface we are
    trying not to have. The refresh token *is* a cookie, and is the only thing
    guarded by the CSRF check.
    """

    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserResponse


class SessionResponse(BaseModel):
    """One active refresh-token family, for the sessions list."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    issued_at: datetime
    expires_at: datetime
    user_agent: str | None
    # Whether this is the session making the request. Shown so nobody revokes
    # the session they are currently using by accident.
    current: bool = False


class MessageResponse(BaseModel):
    message: str


class PasswordChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)
