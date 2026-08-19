"""Tenant isolation across the HTTP surface.

``test_rls.py`` proves the database refuses cross-tenant access. This proves the
API never gets the chance to ask — the tenant scope comes from the signed access
token and from nothing the caller can influence.

Both matter. Database-level isolation without API-level discipline means every
endpoint is one forgotten filter away from a leak that RLS happens to catch;
API-level discipline without RLS means one careless query leaks everything.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import text

from tests.conftest import auth_header, register_user, set_tenant


@pytest.fixture
async def two_accounts(client: httpx.AsyncClient):
    """Two fully separate tenants, each created through the real endpoint."""
    from app.main import app

    alice = await register_user(client)

    # A second client so cookie jars do not bleed between the two accounts.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as other:
        bob = await register_user(other)

    assert alice["user"]["tenant_id"] != bob["user"]["tenant_id"]
    return alice, bob


class TestIdentityIsolation:
    async def test_each_token_resolves_to_its_own_user(
        self, client: httpx.AsyncClient, two_accounts
    ) -> None:
        alice, bob = two_accounts

        alice_me = await client.get("/api/v1/auth/me", headers=auth_header(alice["access_token"]))
        bob_me = await client.get("/api/v1/auth/me", headers=auth_header(bob["access_token"]))

        assert alice_me.json()["email"] == alice["email"]
        assert bob_me.json()["email"] == bob["email"]
        assert alice_me.json()["tenant_id"] != bob_me.json()["tenant_id"]

    async def test_sessions_list_shows_only_your_own(
        self, client: httpx.AsyncClient, two_accounts
    ) -> None:
        alice, bob = two_accounts

        response = await client.get(
            "/api/v1/auth/sessions", headers=auth_header(alice["access_token"])
        )
        assert response.status_code == 200
        sessions = response.json()
        assert len(sessions) == 1

        bob_response = await client.get(
            "/api/v1/auth/sessions", headers=auth_header(bob["access_token"])
        )
        bob_ids = {s["id"] for s in bob_response.json()}
        alice_ids = {s["id"] for s in sessions}
        assert not (alice_ids & bob_ids)


class TestCrossTenantWrites:
    async def test_you_cannot_revoke_another_tenants_session(
        self, client: httpx.AsyncClient, two_accounts
    ) -> None:
        """The classic IDOR, with a real id rather than a guessed one.

        Alice holds Bob's actual session id and a perfectly valid token of her
        own. The only thing stopping her is that her scope is taken from her
        token.
        """
        alice, bob = two_accounts

        bob_sessions = await client.get(
            "/api/v1/auth/sessions", headers=auth_header(bob["access_token"])
        )
        bob_session_id = bob_sessions.json()[0]["id"]

        response = await client.delete(
            f"/api/v1/auth/sessions/{bob_session_id}",
            headers=auth_header(alice["access_token"]),
        )
        assert response.status_code == 404

        # And Bob's session still works.
        still_valid = await client.get(
            "/api/v1/auth/me", headers=auth_header(bob["access_token"])
        )
        assert still_valid.status_code == 200

    async def test_changing_your_password_does_not_touch_another_tenant(
        self, client: httpx.AsyncClient, two_accounts
    ) -> None:
        alice, bob = two_accounts

        response = await client.post(
            "/api/v1/auth/password",
            headers=auth_header(alice["access_token"]),
            json={
                "current_password": alice["password"],
                "new_password": "SomethingElseEntirely4",
            },
        )
        assert response.status_code == 200

        # Bob can still sign in with his original password.
        bob_login = await client.post(
            "/api/v1/auth/login",
            json={"email": bob["email"], "password": bob["password"]},
        )
        assert bob_login.status_code == 200


class TestTokenScopeIntegrity:
    async def test_the_tenant_comes_from_the_token_not_from_the_request(
        self, client: httpx.AsyncClient, two_accounts
    ) -> None:
        """Headers and query parameters must not influence scope.

        A tenant id supplied by the caller is an attacker-controlled value. The
        only reason these are ignored is that nothing reads them — which is
        exactly what this asserts.
        """
        alice, bob = two_accounts

        response = await client.get(
            "/api/v1/auth/me",
            headers={
                **auth_header(alice["access_token"]),
                "X-Tenant-Id": bob["user"]["tenant_id"],
                "X-Tenant": bob["user"]["tenant_id"],
            },
            params={"tenant_id": bob["user"]["tenant_id"]},
        )
        assert response.status_code == 200
        assert response.json()["tenant_id"] == alice["user"]["tenant_id"]

    async def test_a_token_for_a_deleted_user_stops_working_immediately(
        self, client: httpx.AsyncClient, session
    ) -> None:
        """A signed token stays cryptographically valid until it expires.

        For financial data a fifteen-minute window of access for a disabled
        account is not acceptable, so the account is re-checked on every request.
        """
        account = await register_user(client)

        before = await client.get(
            "/api/v1/auth/me", headers=auth_header(account["access_token"])
        )
        assert before.status_code == 200

        async with session.begin():
            await set_tenant(session, account["user"]["tenant_id"])
            await session.execute(
                text("UPDATE users SET status = 'suspended' WHERE id = :id"),
                {"id": account["user"]["id"]},
            )

        after = await client.get(
            "/api/v1/auth/me", headers=auth_header(account["access_token"])
        )
        assert after.status_code == 401
        assert after.json()["error"]["code"] == "account_unavailable"


class TestAuditIsolation:
    async def test_audit_entries_are_written_under_the_acting_tenant(
        self, client: httpx.AsyncClient, session, two_accounts
    ) -> None:
        alice, bob = two_accounts

        async with session.begin():
            await set_tenant(session, alice["user"]["tenant_id"])
            alice_actions = (
                await session.execute(
                    text("SELECT DISTINCT user_id FROM audit_logs")
                )
            ).scalars().all()

        # Under Alice's scope only Alice's entries are visible at all.
        assert alice_actions == [alice["user"]["id"]] or all(
            str(uid) == alice["user"]["id"] for uid in alice_actions
        )
