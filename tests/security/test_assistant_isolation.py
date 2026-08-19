"""Cross-tenant authorization on the assistant path.

The assistant is the only surface where a *model* — untrusted, and steerable by
whoever chose a merchant name on a statement — decides what to look up. So the
question is not whether the tools filter correctly. It is whether there is any
expressible request that reaches another tenant's data.

The answer is meant to be structural: identity is injected server-side from the
access token, and no tool argument, no request field and no phrasing can carry
it. These tests try each of those routes and check that the failure is a
validation error or an empty result, never someone else's money.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text

from app.assistant import executor, orchestrator
from app.db.session import scoped_session

from tests.conftest import auth_header, register_user

MONTH = date(2026, 3, 1)


async def _seed(tenant_id: uuid.UUID, *, merchant: str, amount: str) -> None:
    """One unmistakable transaction, so a leak is impossible to miss."""
    async with scoped_session(tenant_id, actor="system") as session:
        account_id = uuid.uuid4()
        await session.execute(
            text(
                """
                INSERT INTO accounts (
                    id, tenant_id, bank_code, bank_name, account_type, status,
                    account_last4, account_fingerprint
                ) VALUES (
                    :id, :tenant_id, 'HDFC', 'HDFC Bank', 'savings', 'active',
                    '9999', :fingerprint
                )
                """
            ),
            {"id": account_id, "tenant_id": tenant_id, "fingerprint": uuid.uuid4().hex},
        )
        await session.execute(
            text(
                """
                INSERT INTO transactions (
                    id, tenant_id, account_id,
                    original_txn_date, original_description, original_amount,
                    original_direction, original_payment_method, original_merchant,
                    category_source, movement_type, is_expense,
                    confidence_extraction, confidence_merchant,
                    confidence_category, confidence_validation,
                    review_status, fingerprint
                ) VALUES (
                    :id, :tenant_id, :account_id,
                    :txn_date, :description, :amount,
                    'debit', 'card', :merchant,
                    'deterministic_rule', 'expense', true,
                    0.99, 0.99, 0.99, 1.0,
                    'auto_approved', :fingerprint
                )
                """
            ),
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_id,
                "account_id": account_id,
                "txn_date": MONTH.replace(day=15),
                "description": f"POS {merchant}",
                "merchant": merchant,
                "amount": Decimal(amount),
                "fingerprint": uuid.uuid4().hex * 2,
            },
        )


@pytest.fixture
async def two_ledgers(client: httpx.AsyncClient):
    """Two tenants with disjoint, individually identifiable spending."""
    from app.main import app

    alice = await register_user(client)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as other:
        bob = await register_user(other)

    alice_tenant = uuid.UUID(alice["user"]["tenant_id"])
    bob_tenant = uuid.UUID(bob["user"]["tenant_id"])

    await _seed(alice_tenant, merchant="Alicecorp", amount="1111.00")
    await _seed(bob_tenant, merchant="Bobcorp", amount="7777.00")

    return (alice, alice_tenant), (bob, bob_tenant)


class TestToolsCannotReachAnotherTenant:
    async def test_a_tool_sees_only_the_session_it_was_given(self, two_ledgers):
        (_, alice_tenant), (_, bob_tenant) = two_ledgers

        async with scoped_session(alice_tenant) as session:
            execution = await executor.execute(
                session,
                name="get_top_merchants",
                arguments={"period": MONTH.strftime("%Y-%m"), "limit": 20},
                default_month=MONTH,
            )

        names = [row["merchant"] for row in execution.result.display["merchants"]]
        assert "Alicecorp" in names
        assert "Bobcorp" not in names

    async def test_naming_the_other_tenant_changes_nothing(self, two_ledgers):
        """The most direct attempt: ask for the other tenant's merchant by name."""
        (_, alice_tenant), (_, bob_tenant) = two_ledgers

        async with scoped_session(alice_tenant) as session:
            execution = await executor.execute(
                session,
                name="get_transactions",
                arguments={
                    "merchant": "Bobcorp",
                    "period": MONTH.strftime("%Y-%m"),
                    "limit": 25,
                },
                default_month=MONTH,
            )

        view = execution.result.model_view
        assert view["matched_count"] == 0
        assert view["matched_total_rupees"] == 0

    async def test_an_injected_tenant_argument_is_a_validation_error(self, two_ledgers):
        """Not ignored — refused. `extra="forbid"` has nowhere to put it."""
        (_, alice_tenant), (_, bob_tenant) = two_ledgers

        async with scoped_session(alice_tenant) as session:
            execution = await executor.execute(
                session,
                name="get_monthly_spending",
                arguments={"month": "2026-03", "tenant_id": str(bob_tenant)},
                default_month=MONTH,
            )

        assert not execution.ok
        assert execution.error_code == "invalid_arguments"

    async def test_an_answer_quotes_only_the_asking_tenants_figures(self, two_ledgers):
        (_, alice_tenant), (_, bob_tenant) = two_ledgers

        async with scoped_session(alice_tenant) as session:
            answer = await orchestrator.answer(
                session,
                tenant_id=alice_tenant,
                question="Where did most of my money go?",
                suggestion_id="top_merchants",
            )

        assert "Bobcorp" not in answer.text
        assert "7,777" not in answer.text


class TestTheHTTPSurface:
    async def test_a_tenant_id_in_the_body_is_rejected(
        self, client: httpx.AsyncClient, two_ledgers
    ):
        (alice, _), (_, bob_tenant) = two_ledgers

        response = await client.post(
            "/api/v1/assistant/query",
            headers=auth_header(alice["access_token"]),
            json={
                "question": "How much did I spend?",
                "tenant_id": str(bob_tenant),
            },
        )
        assert response.status_code == 422

    async def test_both_endpoints_require_authentication(
        self, client: httpx.AsyncClient
    ):
        for method, path, body in (
            ("post", "/api/v1/assistant/query", {"question": "hello"}),
            ("get", "/api/v1/assistant/suggestions", None),
        ):
            call = getattr(client, method)
            response = await (call(path, json=body) if body else call(path))
            assert response.status_code == 401

    async def test_each_caller_gets_their_own_answer(
        self, client: httpx.AsyncClient, two_ledgers
    ):
        (alice, _), (bob, _) = two_ledgers

        alice_answer = await client.post(
            "/api/v1/assistant/query",
            headers=auth_header(alice["access_token"]),
            json={"suggestion_id": "top_merchants"},
        )
        bob_answer = await client.post(
            "/api/v1/assistant/query",
            headers=auth_header(bob["access_token"]),
            json={"suggestion_id": "top_merchants"},
        )

        alice_text = alice_answer.json()["answer"]
        bob_text = bob_answer.json()["answer"]

        assert "Alicecorp" in alice_text and "Bobcorp" not in alice_text
        assert "Bobcorp" in bob_text and "Alicecorp" not in bob_text
