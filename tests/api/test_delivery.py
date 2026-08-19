"""The delivery surface: export, audit, notifications, categories.

These are the endpoints a user reaches for when they want their data out, want
to know what happened to it, or want to change how it is read. Each has one
property worth testing beyond "it returns 200":

*   an **export** must contain the ledger as the user sees it, and must be
    scoped to the caller;
*   an **audit** entry must be written by the actions that matter and must
    carry no financial content;
*   a **notification** must not repeat itself every time a nightly job runs;
*   a **rule** must outrank everything below it in the cascade.
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import date
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text

from app.db.session import scoped_session

from tests.conftest import auth_header, register_user


@pytest.fixture
async def seeded(client: httpx.AsyncClient):
    """A user with a couple of transactions in their ledger."""
    from tests.conftest import create_account, create_transaction, set_tenant

    user = await register_user(client)
    tenant_id = uuid.UUID(user["user"]["tenant_id"])

    async with scoped_session(tenant_id, actor="system") as session:
        await set_tenant(session, tenant_id)
        account_id = await create_account(session, tenant_id)
        await create_transaction(
            session, tenant_id, account_id,
            amount="1234.56", description="POS SWIGGY BANGALORE",
            txn_date=date(2026, 3, 4),
        )
        await create_transaction(
            session, tenant_id, account_id,
            amount="99.00", description="POS BLINKIT",
            txn_date=date(2026, 3, 6), direction="credit",
        )
    return user, tenant_id


class TestExport:
    async def test_csv_contains_the_rows_and_a_header(self, client, seeded):
        user, _ = seeded
        response = await client.post(
            "/api/v1/export/transactions?format=csv",
            headers=auth_header(user["access_token"]),
            json={},
        )
        assert response.status_code == 200
        assert "text/csv" in response.headers["content-type"]
        assert "attachment" in response.headers["content-disposition"]

        body = response.text.lstrip("﻿")
        rows = list(csv.DictReader(io.StringIO(body)))
        assert len(rows) == 2
        assert rows[0]["Date"] == "2026-03-04"
        assert rows[0]["Amount"] == "1234.56"

    async def test_amounts_are_exact_decimal_strings_not_floats(self, client, seeded):
        """A JSON number would become an IEEE double in the importer."""
        user, _ = seeded
        response = await client.post(
            "/api/v1/export/transactions?format=json",
            headers=auth_header(user["access_token"]),
            json={},
        )
        payload = json.loads(response.text)
        for row in payload["transactions"]:
            assert isinstance(row["amount"], str)
            assert Decimal(row["amount"]) == Decimal(row["amount"]).quantize(
                Decimal("0.01")
            )

    async def test_pdf_is_a_pdf(self, client, seeded):
        user, _ = seeded
        response = await client.post(
            "/api/v1/export/transactions?format=pdf",
            headers=auth_header(user["access_token"]),
            json={},
        )
        assert response.status_code == 200
        assert response.content.startswith(b"%PDF-")

    async def test_filters_narrow_the_file(self, client, seeded):
        user, _ = seeded
        response = await client.post(
            "/api/v1/export/transactions?format=csv",
            headers=auth_header(user["access_token"]),
            json={"direction": "credit"},
        )
        rows = list(csv.DictReader(io.StringIO(response.text.lstrip("﻿"))))
        assert len(rows) == 1
        assert rows[0]["Direction"] == "credit"

    async def test_an_export_is_scoped_to_the_caller(self, client, seeded):
        """The whole point, stated as a test."""
        from app.main import app

        user, _ = seeded
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as other_client:
            stranger = await register_user(other_client)

        response = await client.post(
            "/api/v1/export/transactions?format=csv",
            headers=auth_header(stranger["access_token"]),
            json={},
        )
        rows = list(csv.DictReader(io.StringIO(response.text.lstrip("﻿"))))
        assert rows == []

    async def test_an_unknown_format_is_refused(self, client, seeded):
        user, _ = seeded
        response = await client.post(
            "/api/v1/export/transactions?format=xlsx",
            headers=auth_header(user["access_token"]),
            json={},
        )
        assert response.status_code == 422

    async def test_exporting_is_audited(self, client, seeded):
        """Taking a whole financial history out is exactly the event a user
        should be able to see afterwards."""
        user, _ = seeded
        await client.post(
            "/api/v1/export/transactions?format=csv",
            headers=auth_header(user["access_token"]),
            json={},
        )
        response = await client.get(
            "/api/v1/audit/logs?action=export",
            headers=auth_header(user["access_token"]),
        )
        entries = response.json()["items"]
        assert entries and entries[0]["action"] == "export"
        assert entries[0]["details"]["count"] == 2


class TestAuditLog:
    async def test_registering_and_signing_in_are_recorded(self, client, seeded):
        user, _ = seeded
        response = await client.get(
            "/api/v1/audit/logs", headers=auth_header(user["access_token"])
        )
        actions = {entry["action"] for entry in response.json()["items"]}
        assert "register" in actions

    async def test_no_entry_carries_a_financial_value(self, client, seeded):
        """`details` is restricted when written, not when read — so this is a
        test of the writers, through the endpoint that exposes them."""
        user, _ = seeded
        await client.post(
            "/api/v1/export/transactions?format=csv",
            headers=auth_header(user["access_token"]),
            json={"search": "SWIGGY"},
        )
        response = await client.get(
            "/api/v1/audit/logs", headers=auth_header(user["access_token"])
        )
        rendered = json.dumps(response.json())
        assert "1234.56" not in rendered
        assert "SWIGGY" not in rendered

    async def test_it_is_scoped_to_the_caller(self, client, seeded):
        from app.main import app

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as other_client:
            stranger = await register_user(other_client)

        response = await client.get(
            "/api/v1/audit/logs", headers=auth_header(stranger["access_token"])
        )
        emails = {entry["actor_email"] for entry in response.json()["items"]}
        assert emails <= {stranger["email"], None}


class TestNotifications:
    async def test_an_import_notification_is_not_repeated(self, client, seeded):
        """The nightly jobs re-derive the same findings; the list must not grow."""
        from app.models.enums import NotificationKind
        from app.services import notifications as service

        user, tenant_id = seeded
        resource = uuid.uuid4()

        async with scoped_session(tenant_id, actor="system") as session:
            first = await service.create(
                session, tenant_id=tenant_id, kind=NotificationKind.ANOMALY_DETECTED,
                title="Something stands out", resource_type="anomaly",
                resource_id=resource,
            )
            second = await service.create(
                session, tenant_id=tenant_id, kind=NotificationKind.ANOMALY_DETECTED,
                title="Something stands out, reworded", resource_type="anomaly",
                resource_id=resource,
            )

        assert first is True
        assert second is False

        response = await client.get(
            "/api/v1/notifications", headers=auth_header(user["access_token"])
        )
        assert len(response.json()["items"]) == 1

    async def test_marking_read_clears_the_unread_count(self, client, seeded):
        from app.models.enums import NotificationKind
        from app.services import notifications as service

        user, tenant_id = seeded
        async with scoped_session(tenant_id, actor="system") as session:
            await service.create(
                session, tenant_id=tenant_id,
                kind=NotificationKind.STATEMENT_PROCESSED,
                title="12 transactions imported", resource_id=uuid.uuid4(),
            )

        listing = await client.get(
            "/api/v1/notifications", headers=auth_header(user["access_token"])
        )
        assert listing.json()["unread"] == 1

        await client.post(
            "/api/v1/notifications/read-all",
            headers=auth_header(user["access_token"]),
        )
        after = await client.get(
            "/api/v1/notifications", headers=auth_header(user["access_token"])
        )
        assert after.json()["unread"] == 0


class TestCategoryRules:
    async def test_a_rule_is_created_and_listed(self, client, seeded):
        user, _ = seeded
        created = await client.post(
            "/api/v1/categories/rules",
            headers=auth_header(user["access_token"]),
            json={"merchant_pattern": "Swiggy", "category_slug": "food"},
        )
        assert created.status_code == 200

        listing = await client.get(
            "/api/v1/categories/rules", headers=auth_header(user["access_token"])
        )
        rules = listing.json()
        assert len(rules) == 1
        assert rules[0]["category_slug"] == "food"

    async def test_an_unknown_category_is_refused(self, client, seeded):
        user, _ = seeded
        response = await client.post(
            "/api/v1/categories/rules",
            headers=auth_header(user["access_token"]),
            json={"merchant_pattern": "Swiggy", "category_slug": "spaceships"},
        )
        assert response.status_code == 422

    async def test_a_rule_outranks_the_deterministic_tiers(self, client, seeded):
        """The property that makes a rule worth creating."""
        from app.services import categorization

        user, tenant_id = seeded
        await client.post(
            "/api/v1/categories/rules",
            headers=auth_header(user["access_token"]),
            json={"merchant_pattern": "Blinkit", "category_slug": "entertainment"},
        )

        async with scoped_session(tenant_id) as session:
            match = await categorization._apply_user_rule(
                session, "Blinkit", Decimal("500")
            )

        assert match is not None
        assert match["category_slug"] == "entertainment"

    async def test_creating_and_deleting_are_audited(self, client, seeded):
        user, _ = seeded
        created = await client.post(
            "/api/v1/categories/rules",
            headers=auth_header(user["access_token"]),
            json={"merchant_pattern": "Zomato", "category_slug": "food"},
        )
        await client.delete(
            f"/api/v1/categories/rules/{created.json()['id']}",
            headers=auth_header(user["access_token"]),
        )
        response = await client.get(
            "/api/v1/audit/logs", headers=auth_header(user["access_token"])
        )
        actions = [entry["action"] for entry in response.json()["items"]]
        assert "rule_create" in actions and "rule_delete" in actions

    async def test_the_merchant_pattern_stays_out_of_the_audit_row(
        self, client, seeded
    ):
        """It is the user's own text, and an audit row is exported on request."""
        user, _ = seeded
        await client.post(
            "/api/v1/categories/rules",
            headers=auth_header(user["access_token"]),
            json={"merchant_pattern": "Dr Kulkarni Clinic", "category_slug": "healthcare"},
        )
        response = await client.get(
            "/api/v1/audit/logs?action=rule_create",
            headers=auth_header(user["access_token"]),
        )
        assert "Kulkarni" not in json.dumps(response.json())


class TestAccountErasure:
    async def test_the_confirmation_phrase_is_required(self, client, seeded):
        user, _ = seeded
        response = await client.request(
            "DELETE",
            "/api/v1/auth/account",
            headers=auth_header(user["access_token"]),
            json={"password": user["password"], "confirm": "yes please"},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "confirmation_required"

    async def test_the_password_is_required(self, client, seeded):
        user, _ = seeded
        response = await client.request(
            "DELETE",
            "/api/v1/auth/account",
            headers=auth_header(user["access_token"]),
            json={"password": "not-the-password", "confirm": "DELETE MY DATA"},
        )
        assert response.status_code == 401

    async def test_both_together_erase_everything(self, client, seeded):
        user, tenant_id = seeded
        response = await client.request(
            "DELETE",
            "/api/v1/auth/account",
            headers=auth_header(user["access_token"]),
            json={"password": user["password"], "confirm": "DELETE MY DATA"},
        )
        assert response.status_code == 200

        # The token is signed and still unexpired, but the account behind it is
        # gone — `get_current_user` re-checks the user on every request, which
        # is what makes erasure take effect immediately rather than in fifteen
        # minutes.
        after = await client.get(
            "/api/v1/auth/me", headers=auth_header(user["access_token"])
        )
        assert after.status_code == 401

        async with scoped_session(tenant_id) as session:
            remaining = (
                await session.execute(text("SELECT count(*) FROM transactions"))
            ).scalar_one()
        assert remaining == 0


class TestTheAuditDetailAllowList:
    """The perimeter on what an audit row may carry.

    Worth its own tests because it is silent by design: an unlisted key is
    dropped, not rejected, so a call site that uses the wrong name produces an
    audit row that is quietly emptier than intended. That happened while
    building this phase — the export endpoint recorded ``rows`` where the
    allow-list says ``count``, and nothing complained.
    """

    async def test_an_unlisted_key_is_dropped_and_recorded_as_dropped(self, seeded):
        from app.models.enums import AuditAction
        from app.services import audit

        _, tenant_id = seeded
        async with scoped_session(tenant_id, actor="system") as session:
            await audit.record(
                session,
                tenant_id=tenant_id,
                action=AuditAction.EXPORT,
                details={"count": 3, "merchant": "Swiggy", "amount": "1234.56"},
            )
            row = (
                await session.execute(
                    text(
                        "SELECT details FROM audit_logs "
                        "WHERE action = 'export' ORDER BY occurred_at DESC LIMIT 1"
                    )
                )
            ).scalar_one()

        assert row["count"] == 3
        assert "merchant" not in row
        assert "amount" not in row
        # Named, not silently vanished: an operator reading the row can see that
        # something was withheld and go and look at the call site.
        assert set(row["_dropped"]) == {"amount", "merchant"}

    async def test_the_dropped_names_do_not_carry_the_values(self, seeded):
        from app.models.enums import AuditAction
        from app.services import audit

        _, tenant_id = seeded
        async with scoped_session(tenant_id, actor="system") as session:
            await audit.record(
                session,
                tenant_id=tenant_id,
                action=AuditAction.EXPORT,
                details={"description": "IMPS-412312345678-RAHUL SHARMA"},
            )
            row = (
                await session.execute(
                    text(
                        "SELECT details FROM audit_logs "
                        "WHERE action = 'export' ORDER BY occurred_at DESC LIMIT 1"
                    )
                )
            ).scalar_one()

        assert "RAHUL" not in json.dumps(row)
        assert "412312345678" not in json.dumps(row)
