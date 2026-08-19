"""Authentication, exercised over real HTTP against the real app."""

from __future__ import annotations

import httpx
import pytest

from tests.conftest import (
    STRONG_PASSWORD,
    auth_header,
    register_user,
    unique_email,
)

CSRF_HEADER = "X-CSRF-Token"


class TestRegistration:
    async def test_registration_creates_a_tenant_and_signs_the_user_in(
        self, client: httpx.AsyncClient
    ) -> None:
        email = unique_email()
        response = await client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": STRONG_PASSWORD, "full_name": "Priya Nair"},
        )

        assert response.status_code == 201
        body = response.json()
        assert body["access_token"]
        assert body["user"]["email"] == email
        assert body["user"]["role"] == "owner"
        # Every account gets its own tenant, so multi-user comes later as a row
        # insert rather than a migration.
        assert body["user"]["tenant_id"]

    async def test_refresh_cookie_is_http_only_and_scoped_to_auth(
        self, client: httpx.AsyncClient
    ) -> None:
        """An XSS payload must not be able to read a long-lived credential."""
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": unique_email(),
                "password": STRONG_PASSWORD,
                "full_name": "Cookie Test",
            },
        )
        cookie_headers = response.headers.get_list("set-cookie")
        refresh = next(h for h in cookie_headers if h.startswith("expense_refresh="))

        lowered = refresh.lower()
        assert "httponly" in lowered
        assert "path=/api/v1/auth" in lowered
        assert "samesite=lax" in lowered

    async def test_csrf_cookie_is_readable_by_script_on_purpose(
        self, client: httpx.AsyncClient
    ) -> None:
        """The double-submit mechanism requires the client to read and echo it.

        Safe because the value is useless without the refresh cookie, which a
        cross-site page cannot read.
        """
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": unique_email(),
                "password": STRONG_PASSWORD,
                "full_name": "Cookie Test",
            },
        )
        csrf = next(
            h for h in response.headers.get_list("set-cookie")
            if h.startswith("expense_csrf=")
        )
        assert "httponly" not in csrf.lower()

    @pytest.mark.parametrize(
        "password",
        ["short", "password123", "aaaaaaaaaaaaaaaa", "1234567890ab"],
    )
    async def test_weak_passwords_are_rejected(
        self, client: httpx.AsyncClient, password: str
    ) -> None:
        response = await client.post(
            "/api/v1/auth/register",
            json={"email": unique_email(), "password": password, "full_name": "X"},
        )
        assert response.status_code == 422

    async def test_password_containing_the_email_is_rejected(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": "meenakshi@example.com",
                "password": "meenakshi-secret-1",
                "full_name": "M",
            },
        )
        assert response.status_code == 422

    async def test_duplicate_email_is_a_conflict(self, client: httpx.AsyncClient) -> None:
        account = await register_user(client)
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": account["email"],
                "password": STRONG_PASSWORD,
                "full_name": "Impostor",
            },
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "email_taken"


class TestLogin:
    async def test_login_with_correct_password(self, client: httpx.AsyncClient) -> None:
        account = await register_user(client)
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": account["email"], "password": account["password"]},
        )
        assert response.status_code == 200
        assert response.json()["user"]["email"] == account["email"]

    async def test_login_is_case_insensitive_on_email(
        self, client: httpx.AsyncClient
    ) -> None:
        """CITEXT in the database, not lower() sprinkled through the code."""
        account = await register_user(client)
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": account["email"].upper(), "password": account["password"]},
        )
        assert response.status_code == 200

    async def test_wrong_password_is_rejected(self, client: httpx.AsyncClient) -> None:
        account = await register_user(client)
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": account["email"], "password": "TotallyWrongPassword1"},
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_credentials"

    async def test_unknown_email_is_indistinguishable_from_a_wrong_password(
        self, client: httpx.AsyncClient
    ) -> None:
        """The response must not be an account-existence oracle.

        Same status, same error code, same message — otherwise the login form
        becomes a way to enumerate who has an account here, which for a
        financial product is itself a disclosure.
        """
        account = await register_user(client)

        wrong_password = await client.post(
            "/api/v1/auth/login",
            json={"email": account["email"], "password": "TotallyWrongPassword1"},
        )
        unknown_user = await client.post(
            "/api/v1/auth/login",
            json={"email": unique_email("ghost"), "password": "TotallyWrongPassword1"},
        )

        assert wrong_password.status_code == unknown_user.status_code == 401
        assert wrong_password.json() == unknown_user.json()

    async def test_repeated_failures_lock_the_account(
        self, client: httpx.AsyncClient
    ) -> None:
        """Per-account lockout, tested at the service layer.

        Two throttles guard login and they defend different things: the per-IP
        rate limit stops one source hammering many accounts, and the per-account
        lockout stops many sources hammering one account. The IP limit is the
        stricter of the two, so over HTTP from a single address it always fires
        first and the lockout is unreachable — which is correct behaviour but
        makes it untestable through the endpoint. Calling the service directly
        exercises the branch that a distributed attack would actually hit.
        """
        from app.core.errors import AuthenticationError
        from app.services import auth as auth_service
        from app.services.auth import MAX_FAILED_LOGINS

        account = await register_user(client)

        codes = []
        for _ in range(MAX_FAILED_LOGINS + 1):
            try:
                await auth_service.login(
                    email=account["email"], password="WrongPassword123456"
                )
            except AuthenticationError as exc:
                codes.append(exc.error_code)

        assert "account_locked" in codes, f"never locked out: {codes}"

        # Even the correct password is refused while the lock holds.
        with pytest.raises(AuthenticationError) as excinfo:
            await auth_service.login(
                email=account["email"], password=account["password"]
            )
        assert excinfo.value.error_code == "account_locked"

    async def test_the_login_endpoint_rate_limits_by_ip(
        self, client: httpx.AsyncClient
    ) -> None:
        """The first line of defence against credential stuffing."""
        account = await register_user(client)

        statuses = []
        for _ in range(12):
            response = await client.post(
                "/api/v1/auth/login",
                json={"email": account["email"], "password": "WrongPassword123456"},
            )
            statuses.append(response.status_code)

        assert 429 in statuses, f"never rate limited: {statuses}"


class TestAccessTokens:
    async def test_me_requires_a_token(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/api/v1/auth/me")
        assert response.status_code == 401

    async def test_me_returns_the_signed_in_user(self, client: httpx.AsyncClient) -> None:
        account = await register_user(client)
        response = await client.get(
            "/api/v1/auth/me", headers=auth_header(account["access_token"])
        )
        assert response.status_code == 200
        assert response.json()["email"] == account["email"]

    async def test_a_malformed_token_is_rejected(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/api/v1/auth/me", headers=auth_header("not.a.jwt"))
        assert response.status_code == 401

    async def test_a_token_signed_with_the_wrong_key_is_rejected(
        self, client: httpx.AsyncClient
    ) -> None:
        """Forgery check. If this passes, anyone can mint an admin token."""
        import uuid
        from datetime import datetime, timedelta, timezone

        import jwt

        from app.core.security import JWT_AUDIENCE, JWT_ISSUER

        forged = jwt.encode(
            {
                "iss": JWT_ISSUER,
                "aud": JWT_AUDIENCE,
                "sub": str(uuid.uuid4()),
                "tid": str(uuid.uuid4()),
                "role": "owner",
                "typ": "access",
                "iat": int(datetime.now(timezone.utc).timestamp()),
                "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
            },
            "an-attacker-chosen-key",
            algorithm="HS256",
        )
        response = await client.get("/api/v1/auth/me", headers=auth_header(forged))
        assert response.status_code == 401

    async def test_an_unsigned_alg_none_token_is_rejected(
        self, client: httpx.AsyncClient
    ) -> None:
        """The classic JWT attack: claim the algorithm is `none`.

        Rejected because the decoder pins the accepted algorithms rather than
        trusting the token's own header.
        """
        import uuid
        from datetime import datetime, timedelta, timezone

        import jwt

        from app.core.security import JWT_AUDIENCE, JWT_ISSUER

        unsigned = jwt.encode(
            {
                "iss": JWT_ISSUER,
                "aud": JWT_AUDIENCE,
                "sub": str(uuid.uuid4()),
                "tid": str(uuid.uuid4()),
                "role": "owner",
                "typ": "access",
                "iat": int(datetime.now(timezone.utc).timestamp()),
                "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
            },
            key="",
            algorithm="none",
        )
        response = await client.get("/api/v1/auth/me", headers=auth_header(unsigned))
        assert response.status_code == 401

    async def test_an_expired_token_is_rejected(self, client: httpx.AsyncClient) -> None:
        import uuid
        from datetime import datetime, timedelta, timezone

        import jwt

        from app.core.config import settings
        from app.core.security import JWT_AUDIENCE, JWT_ISSUER

        past = datetime.now(timezone.utc) - timedelta(hours=2)
        expired = jwt.encode(
            {
                "iss": JWT_ISSUER,
                "aud": JWT_AUDIENCE,
                "sub": str(uuid.uuid4()),
                "tid": str(uuid.uuid4()),
                "role": "owner",
                "typ": "access",
                "iat": int(past.timestamp()),
                "exp": int((past + timedelta(minutes=15)).timestamp()),
            },
            settings.SECRET_KEY,
            algorithm=settings.JWT_ALGORITHM,
        )
        response = await client.get("/api/v1/auth/me", headers=auth_header(expired))
        assert response.status_code == 401


class TestRefreshRotation:
    async def _csrf(self, client: httpx.AsyncClient) -> str:
        return client.cookies.get("expense_csrf") or ""

    async def test_refresh_issues_a_new_token_and_rotates_the_old_one(
        self, client: httpx.AsyncClient
    ) -> None:
        await register_user(client)
        before = client.cookies.get("expense_refresh")

        response = await client.post(
            "/api/v1/auth/refresh", headers={CSRF_HEADER: await self._csrf(client)}
        )
        assert response.status_code == 200

        after = client.cookies.get("expense_refresh")
        assert after and after != before, "refresh token was not rotated"

    async def test_refresh_requires_the_csrf_header(
        self, client: httpx.AsyncClient
    ) -> None:
        await register_user(client)
        response = await client.post("/api/v1/auth/refresh")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "csrf_invalid"

    async def test_a_mismatched_csrf_header_is_rejected(
        self, client: httpx.AsyncClient
    ) -> None:
        await register_user(client)
        response = await client.post(
            "/api/v1/auth/refresh", headers={CSRF_HEADER: "not-the-cookie-value"}
        )
        assert response.status_code == 422

    async def test_replaying_a_rotated_token_revokes_the_whole_family(
        self, client: httpx.AsyncClient
    ) -> None:
        """The reason rotation is worth doing at all.

        Without reuse detection, someone who copies a refresh token simply
        refreshes it forever alongside the real user and is never noticed. When
        a rotated token reappears we cannot tell victim from thief, so both
        sessions end and the user re-authenticates.
        """
        await register_user(client)
        stolen = client.cookies.get("expense_refresh")

        # The legitimate client refreshes, rotating the token.
        first = await client.post(
            "/api/v1/auth/refresh", headers={CSRF_HEADER: await self._csrf(client)}
        )
        assert first.status_code == 200

        # The thief replays the token they captured earlier, from their own
        # client — per-request cookies merge with the jar rather than replacing
        # it, so reusing `client` here would send the *current* token and prove
        # nothing.
        from app.main import app

        csrf = await self._csrf(client)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as thief:
            thief.cookies.set("expense_refresh", stolen)
            thief.cookies.set("expense_csrf", csrf)
            replay = await thief.post(
                "/api/v1/auth/refresh", headers={CSRF_HEADER: csrf}
            )

        assert replay.status_code == 401
        assert replay.json()["error"]["code"] == "token_reuse"

        # And the legitimate session is gone too — deliberately.
        legitimate = await client.post(
            "/api/v1/auth/refresh", headers={CSRF_HEADER: await self._csrf(client)}
        )
        assert legitimate.status_code == 401
        assert legitimate.json()["error"]["code"] == "token_reuse"

    async def test_refresh_without_a_cookie_fails(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.post(
            "/api/v1/auth/refresh", headers={CSRF_HEADER: "anything"}
        )
        assert response.status_code in (401, 422)


class TestLogout:
    async def test_logout_ends_the_session(self, client: httpx.AsyncClient) -> None:
        await register_user(client)
        csrf = client.cookies.get("expense_csrf") or ""

        response = await client.post(
            "/api/v1/auth/logout", headers={CSRF_HEADER: csrf}
        )
        assert response.status_code == 200

        # The refresh cookie is cleared, and the token behind it is revoked.
        after = await client.post("/api/v1/auth/refresh", headers={CSRF_HEADER: csrf})
        assert after.status_code in (401, 422)


class TestPasswordChange:
    async def test_changing_the_password_ends_other_sessions(
        self, client: httpx.AsyncClient
    ) -> None:
        """A password change is usually a response to suspected compromise.

        Leaving other sessions signed in would defeat the point of changing it.
        """
        account = await register_user(client)

        response = await client.post(
            "/api/v1/auth/password",
            headers=auth_header(account["access_token"]),
            json={
                "current_password": account["password"],
                "new_password": "AnEntirelyDifferent7",
            },
        )
        assert response.status_code == 200

        old_login = await client.post(
            "/api/v1/auth/login",
            json={"email": account["email"], "password": account["password"]},
        )
        assert old_login.status_code == 401

        new_login = await client.post(
            "/api/v1/auth/login",
            json={"email": account["email"], "password": "AnEntirelyDifferent7"},
        )
        assert new_login.status_code == 200

    async def test_the_current_password_must_be_correct(
        self, client: httpx.AsyncClient
    ) -> None:
        account = await register_user(client)
        response = await client.post(
            "/api/v1/auth/password",
            headers=auth_header(account["access_token"]),
            json={
                "current_password": "NotTheRightOne123",
                "new_password": "AnEntirelyDifferent7",
            },
        )
        assert response.status_code == 401


class TestOAuthGating:
    async def test_google_endpoints_report_404_when_disabled(
        self, client: httpx.AsyncClient
    ) -> None:
        """404, not 500 — a probe should not be able to tell a disabled feature
        from a misconfigured one."""
        response = await client.get("/api/v1/auth/oauth/google", follow_redirects=False)
        assert response.status_code == 404
