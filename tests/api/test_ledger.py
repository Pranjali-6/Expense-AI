"""The ledger, end to end: extraction → reconciliation → dedup → API.

Drives the real pipeline over a real golden fixture and a real database, then
reads the result back through HTTP. The headline assertion is the phase's
definition of done: **re-importing the same statement adds zero rows.**
"""

from __future__ import annotations

import re
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from app.db.session import scoped_session
from app.extraction.pipeline import parse_document
from app.models.enums import ReviewStatus, TrustStatus
from app.services import ledger

from tests.conftest import auth_header, register_user


def mutating_headers(client, user: dict) -> dict[str, str]:
    """Auth plus the CSRF echo that state-changing requests require.

    Double-submit: the cookie is deliberately script-readable and the client
    echoes it in a header. A request without the echo is rejected, which is the
    whole mechanism — so the tests send it explicitly rather than disabling the
    check.
    """
    return {
        **auth_header(user["access_token"]),
        "X-CSRF-Token": client.cookies.get("expense_csrf") or "",
    }

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "statements"


def expected_count(fixture: str) -> int:
    """How many transactions the fixture's ground truth contains.

    Read from `expected.json` rather than hardcoded. The literal 54 was correct
    until the generator changed and then quietly wrong in five tests at once —
    and the invariant was never "54", it was "every transaction in the ground
    truth reached the ledger".
    """
    import json

    truth = json.loads((FIXTURES / f"{fixture}.expected.json").read_text())
    return len(truth["transactions"])

pytestmark = pytest.mark.skipif(
    not (FIXTURES / "hdfc-2024-03.pdf").exists(),
    reason="run `make gen-fixtures` first",
)


async def _import(session, *, tenant_id: uuid.UUID, fixture: str) -> ledger.LedgerResult:
    """Run a fixture through the full trust chain into the ledger."""
    statement_id = uuid.uuid4()
    await session.execute(
        text(
            """
            INSERT INTO statements (
                id, tenant_id, storage_key, file_size_bytes, file_sha256,
                document_type, status, trust_status, page_count
            ) VALUES (
                :id, :tenant_id, :key, 1000, :digest,
                'unknown', 'processing', 'pending', 3
            )
            """
        ),
        {
            "id": statement_id,
            "tenant_id": tenant_id,
            "key": f"test/{statement_id}.pdf",
            "digest": uuid.uuid4().hex + uuid.uuid4().hex[:32],
        },
    )

    outcome = parse_document((FIXTURES / f"{fixture}.pdf").read_bytes())
    return await ledger.persist(
        session, tenant_id=tenant_id, statement_id=statement_id, outcome=outcome
    )


@pytest.fixture
async def owner(client):
    """A registered user, which also creates the tenant to import into."""
    return await register_user(client)


@pytest.fixture
def tenant(owner) -> uuid.UUID:
    return uuid.UUID(owner["user"]["tenant_id"])


@pytest.fixture
async def imported(tenant):
    """One HDFC statement, fully imported through the real trust chain."""
    async with scoped_session(tenant, actor="system") as session:
        result = await _import(session, tenant_id=tenant, fixture="hdfc-2024-03")
    return result


class TestImportingAStatement:
    async def test_every_transaction_reaches_the_ledger(self, imported):
        assert imported.inserted == expected_count("hdfc-2024-03")
        assert imported.duplicates == 0

    async def test_a_statement_that_reconciles_becomes_trusted(self, imported):
        assert imported.report.reconciles is True
        assert imported.report.delta == Decimal("0.00")
        assert imported.trust_status == TrustStatus.TRUSTED

    async def test_an_account_is_created_from_the_statement(self, tenant, imported):
        async with scoped_session(tenant) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT bank_code, account_last4, account_type "
                        "FROM accounts WHERE id = :id"
                    ),
                    {"id": imported.account_id},
                )
            ).one()
        assert row.bank_code == "HDFC"
        assert len(row.account_last4) == 4

    async def test_no_full_account_number_is_stored_anywhere(self, tenant, imported):
        """The point of storing only four digits.

        `account_fingerprint` is excluded deliberately: it is an HMAC of
        (tenant, bank, type, last4) under the deployment key, so it is *meant*
        to be a long opaque string and reveals nothing about the account number.
        Every other column is checked.
        """
        async with scoped_session(tenant) as session:
            row = (
                await session.execute(
                    text("SELECT * FROM accounts WHERE id = :id"),
                    {"id": imported.account_id},
                )
            ).one()

        assert len(row.account_last4) == 4
        assert row.account_last4.isdigit()

        for column, value in row._mapping.items():
            if column == "account_fingerprint" or not isinstance(value, str):
                continue
            runs = re.findall(r"\d{6,}", value)
            assert not runs, f"{column} holds an account-number-shaped digit run"

    async def test_confidence_is_scored_on_every_row(self, tenant, imported):
        async with scoped_session(tenant) as session:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT count(*) AS total,
                               count(*) FILTER (WHERE confidence_min IS NULL) AS unscored,
                               count(*) FILTER (
                                   WHERE confidence_min <> LEAST(
                                       confidence_extraction, confidence_merchant,
                                       confidence_category, confidence_validation)
                               ) AS drifted
                        FROM transactions
                        """
                    )
                )
            ).one()
        assert row.total == expected_count("hdfc-2024-03")
        assert row.unscored == 0
        # confidence_min is a generated column; it cannot disagree with LEAST().
        assert row.drifted == 0

    async def test_internal_movement_is_excluded_from_spending(self, tenant, imported):
        async with scoped_session(tenant) as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT movement_type, is_expense
                        FROM transactions
                        WHERE movement_type IN
                              ('credit_card_payment', 'cash_withdrawal', 'transfer',
                               'salary', 'refund', 'investment')
                        """
                    )
                )
            ).all()
        assert rows, "the fixture contains internal movement and none was classified"
        assert all(row.is_expense is False for row in rows)


class TestDuplicateImports:
    """The definition of done for this phase."""

    async def test_reimporting_the_same_statement_adds_zero_rows(self, tenant, imported):
        async with scoped_session(tenant, actor="system") as session:
            again = await _import(session, tenant_id=tenant, fixture="hdfc-2024-03")

        assert again.inserted == 0
        assert again.duplicates == expected_count("hdfc-2024-03")

        async with scoped_session(tenant) as session:
            total = (
                await session.execute(text("SELECT count(*) FROM transactions"))
            ).scalar_one()
        assert total == expected_count("hdfc-2024-03")

    async def test_a_reissued_copy_adds_zero_rows(self, tenant, imported):
        """Different PDF bytes, identical transactions.

        Content-hash detection cannot catch this one — only the transaction
        fingerprint can, which is what makes the fingerprint load-bearing
        rather than an optimisation.
        """
        async with scoped_session(tenant, actor="system") as session:
            reissue = await _import(
                session, tenant_id=tenant, fixture="hdfc-2024-03-reissued"
            )

        assert reissue.inserted == 0
        assert reissue.duplicates == expected_count("hdfc-2024-03")

    async def test_a_different_statement_still_imports(self, tenant, imported):
        """Deduplication must not become a wall that blocks real data."""
        async with scoped_session(tenant, actor="system") as session:
            other = await _import(session, tenant_id=tenant, fixture="icici-2024-03")

        assert other.inserted == expected_count("icici-2024-03")
        assert other.duplicates == 0


class TestLedgerOverHttp:
    async def test_transactions_are_listed_with_their_confidence(
        self, client, owner, tenant, imported
    ):
        user = owner
        response = await client.get(
            "/api/v1/transactions?limit=5", headers=auth_header(user['access_token'])
        )
        assert response.status_code == 200

        body = response.json()
        assert body["total"] == expected_count("hdfc-2024-03")
        row = body["items"][0]
        # Money crosses as a string, never a JSON number.
        assert isinstance(row["amount"], str)
        assert isinstance(row["confidence_min"], str)
        assert row["review_status"] in {status.value for status in ReviewStatus}

    async def test_filters_narrow_the_result_set(self, client, owner, tenant, imported):
        user = owner
        response = await client.get(
            "/api/v1/transactions?category=food&limit=100", headers=auth_header(user['access_token'])
        )
        items = response.json()["items"]
        assert items
        assert all(row["category_slug"] == "food" for row in items)

    async def test_the_explanation_names_the_tier_that_decided(
        self, client, owner, tenant, imported
    ):
        user = owner
        listing = await client.get(
            "/api/v1/transactions?category=cash_withdrawal&limit=1",
            headers=auth_header(user['access_token']),
        )
        transaction_id = listing.json()["items"][0]["id"]

        response = await client.get(
            f"/api/v1/transactions/{transaction_id}/explain", headers=auth_header(user['access_token'])
        )
        body = response.json()

        assert body["source"] == "deterministic_rule"
        assert "rule matched" in body["sentence"].lower()
        assert body["provenance"]["page"] is not None
        assert body["confidence"]["weakest"] is not None

    async def test_a_correction_never_touches_the_original(
        self, client, owner, tenant, imported
    ):
        """The guarantee that makes a disputed transaction arguable later."""
        user = owner
        listing = await client.get(
            "/api/v1/transactions?category=food&limit=1", headers=auth_header(user['access_token'])
        )
        row = listing.json()["items"][0]

        response = await client.patch(
            f"/api/v1/transactions/{row['id']}",
            json={"category_slug": "grocery", "merchant": "Corrected Name"},
            headers=mutating_headers(client, user),
        )
        assert response.status_code == 200
        updated = response.json()

        assert updated["merchant"] == "Corrected Name"
        assert updated["category_slug"] == "grocery"
        assert updated["category_source"] == "user_rule"
        assert updated["is_verified"] is True
        # The frozen columns still hold what the bank printed.
        assert updated["original_merchant"] == row["merchant"]
        assert updated["original_amount"] == row["amount"]

    async def test_a_correction_is_recorded_in_the_audit_trail(
        self, client, owner, tenant, imported
    ):
        user = owner
        listing = await client.get(
            "/api/v1/transactions?category=food&limit=1", headers=auth_header(user['access_token'])
        )
        transaction_id = listing.json()["items"][0]["id"]

        await client.patch(
            f"/api/v1/transactions/{transaction_id}",
            json={"category_slug": "travel"},
            headers=mutating_headers(client, user),
        )
        response = await client.get(
            f"/api/v1/transactions/{transaction_id}/audit", headers=auth_header(user['access_token'])
        )
        entries = response.json()

        assert entries
        assert entries[0]["actor_kind"] == "user"
        assert entries[0]["field_name"] == "category"

    async def test_apply_to_similar_leaves_verified_rows_alone(
        self, client, owner, tenant, imported
    ):
        """A previous human decision is never overwritten by a bulk action."""
        user = owner
        listing = await client.get(
            "/api/v1/transactions?merchant=Swiggy&limit=10", headers=auth_header(user['access_token'])
        )
        rows = listing.json()["items"]
        assert len(rows) >= 3, "the fixture should contain several Swiggy rows"

        # Pin one row to a category the user chose.
        await client.patch(
            f"/api/v1/transactions/{rows[0]['id']}",
            json={"category_slug": "entertainment"},
            headers=mutating_headers(client, user),
        )

        # Now bulk-apply a different category from another row.
        await client.post(
            f"/api/v1/transactions/{rows[1]['id']}/apply-to-similar",
            json={"category_slug": "travel"},
            headers=mutating_headers(client, user),
        )

        pinned = await client.get(
            f"/api/v1/transactions/{rows[0]['id']}", headers=auth_header(user['access_token'])
        )
        assert pinned.json()["category_slug"] == "entertainment"

    async def test_review_stats_count_only_what_needs_attention(
        self, client, owner, tenant, imported
    ):
        user = owner
        response = await client.get(
            "/api/v1/transactions/review/stats", headers=auth_header(user['access_token'])
        )
        stats = response.json()

        total = expected_count("hdfc-2024-03")
        assert stats["total"] == total
        assert (
            stats["auto_approved"] + stats["flagged"] + stats["review_required"] == total
        )
        # A clean, reconciling statement should mostly clear the gate.
        assert stats["auto_approved"] > stats["review_required"]

    async def test_accounts_are_listed_without_inflated_counts(
        self, client, owner, tenant, imported
    ):
        """Joining transactions and statements multiplies rows without DISTINCT."""
        user = owner
        response = await client.get("/api/v1/accounts", headers=auth_header(user['access_token']))
        accounts = response.json()

        assert len(accounts) == 1
        assert accounts[0]["transaction_count"] == expected_count("hdfc-2024-03")
        assert accounts[0]["statement_count"] == 1


class TestTenantIsolation:
    async def test_another_tenant_sees_none_of_it(self, client, owner, tenant, imported):
        """RLS, not a WHERE clause someone remembered to write."""
        stranger = await register_user(client)

        listing = await client.get("/api/v1/transactions", headers=auth_header(stranger['access_token']))
        assert listing.json()["total"] == 0

        accounts = await client.get("/api/v1/accounts", headers=auth_header(stranger['access_token']))
        assert accounts.json() == []

    async def test_a_transaction_id_from_another_tenant_is_a_404(
        self, client, owner, tenant, imported
    ):
        """404 rather than 403 — a 403 would confirm the id exists."""
        user = owner
        listing = await client.get(
            "/api/v1/transactions?limit=1", headers=auth_header(user['access_token'])
        )
        transaction_id = listing.json()["items"][0]["id"]

        stranger = await register_user(client)
        response = await client.get(
            f"/api/v1/transactions/{transaction_id}", headers=auth_header(stranger['access_token'])
        )
        assert response.status_code == 404

    async def test_another_tenant_cannot_correct_your_transaction(
        self, client, owner, tenant, imported
    ):
        user = owner
        listing = await client.get(
            "/api/v1/transactions?limit=1", headers=auth_header(user['access_token'])
        )
        transaction_id = listing.json()["items"][0]["id"]

        stranger = await register_user(client)
        response = await client.patch(
            f"/api/v1/transactions/{transaction_id}",
            json={"category_slug": "other"},
            headers=mutating_headers(client, stranger),
        )
        assert response.status_code == 404
