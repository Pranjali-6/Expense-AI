"""Ledger integrity guarantees, verified against the database itself.

Each of these is a promise the architecture makes. A promise kept only by
application code is a promise that lasts until the first bulk UPDATE written in
a hurry, so all of them are enforced by constraints, triggers or generated
columns — and tested here by trying to break them.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import ActorKind
from tests.conftest import (
    create_account,
    create_tenant,
    create_transaction,
    create_user,
    allow_audit_purge,
    set_actor,
    set_tenant,
    verify_transaction,
)


@pytest.fixture
async def ledger(session: AsyncSession):
    """One tenant with a user and an account, cleaned up afterwards.

    A real user is needed because `verified_has_actor` refuses a transaction
    marked verified with nobody accountable for it.
    """
    async with session.begin():
        tenant_id = await create_tenant(session, "Ledger Test")
        user_id = await create_user(session, tenant_id)
        account_id = await create_account(session, tenant_id)

    yield tenant_id, account_id, user_id

    async with session.begin():
        await set_tenant(session, tenant_id)
        await allow_audit_purge(session)
        for table in ("transaction_audit", "transactions", "accounts", "users"):
            await session.execute(
                text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant_id}
            )
        await session.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})


class TestImmutableOriginals:
    async def test_original_amount_cannot_be_changed(self, session, ledger) -> None:
        tenant_id, account_id, user_id = ledger

        async with session.begin():
            await set_tenant(session, tenant_id)
            txn_id = await create_transaction(
                session, tenant_id, account_id, amount="450.00"
            )

        with pytest.raises(DBAPIError) as excinfo:
            async with session.begin():
                await set_tenant(session, tenant_id)
                await session.execute(
                    text("UPDATE transactions SET original_amount = 999 WHERE id = :id"),
                    {"id": txn_id},
                )

        assert "immutable" in str(excinfo.value).lower()

    async def test_original_description_cannot_be_changed(self, session, ledger) -> None:
        tenant_id, account_id, user_id = ledger

        async with session.begin():
            await set_tenant(session, tenant_id)
            txn_id = await create_transaction(session, tenant_id, account_id)

        with pytest.raises(DBAPIError):
            async with session.begin():
                await set_tenant(session, tenant_id)
                await session.execute(
                    text(
                        "UPDATE transactions SET original_description = 'rewritten' "
                        "WHERE id = :id"
                    ),
                    {"id": txn_id},
                )

    async def test_corrections_are_allowed_and_leave_the_original_intact(
        self, session, ledger
    ) -> None:
        """A correction is written beside the original, never over it."""
        tenant_id, account_id, user_id = ledger

        async with session.begin():
            await set_tenant(session, tenant_id)
            txn_id = await create_transaction(
                session, tenant_id, account_id, amount="450.00",
                description="UPI-SWIGGYINSTAMART-8829172",
            )

        async with session.begin():
            await set_tenant(session, tenant_id)
            await session.execute(
                text(
                    "UPDATE transactions SET corrected_merchant = 'Swiggy Instamart', "
                    "corrected_amount = 460.00 WHERE id = :id"
                ),
                {"id": txn_id},
            )

        async with session.begin():
            await set_tenant(session, tenant_id)
            row = (
                await session.execute(
                    text(
                        "SELECT original_amount, corrected_amount, amount, "
                        "original_description, description "
                        "FROM transactions WHERE id = :id"
                    ),
                    {"id": txn_id},
                )
            ).one()

        assert row.original_amount == Decimal("450.00"), "the original was overwritten"
        assert row.corrected_amount == Decimal("460.00")
        assert row.amount == Decimal("460.00"), "effective value did not follow the correction"
        assert row.original_description == "UPI-SWIGGYINSTAMART-8829172"
        assert row.description == "UPI-SWIGGYINSTAMART-8829172"


class TestGeneratedColumns:
    async def test_effective_value_falls_back_to_the_original(self, session, ledger) -> None:
        tenant_id, account_id, user_id = ledger

        async with session.begin():
            await set_tenant(session, tenant_id)
            txn_id = await create_transaction(
                session, tenant_id, account_id, amount="1234.56", direction="debit"
            )

            row = (
                await session.execute(
                    text(
                        "SELECT amount, direction, txn_date, payment_method "
                        "FROM transactions WHERE id = :id"
                    ),
                    {"id": txn_id},
                )
            ).one()

        assert row.amount == Decimal("1234.56")
        assert row.direction == "debit"
        assert row.txn_date == date(2026, 3, 15)
        assert row.payment_method == "unknown"

    async def test_confidence_min_is_the_lowest_dimension_not_the_average(
        self, session, ledger
    ) -> None:
        """The whole point of four scores.

        Average of (0.99, 0.99, 0.62, 0.99) is 0.90 — comfortably above the
        review threshold, and completely wrong: the category is a coin flip.
        LEAST returns 0.62 and the row goes to a human.
        """
        tenant_id, account_id, user_id = ledger

        async with session.begin():
            await set_tenant(session, tenant_id)
            txn_id = await create_transaction(
                session, tenant_id, account_id,
                confidences=("0.990", "0.990", "0.620", "0.990"),
            )

            result = await session.scalar(
                text("SELECT confidence_min FROM transactions WHERE id = :id"),
                {"id": txn_id},
            )

        assert result == Decimal("0.620")
        assert result != Decimal("0.898"), "confidence_min is averaging, not minimising"

    async def test_generated_column_cannot_be_written_directly(
        self, session, ledger
    ) -> None:
        tenant_id, account_id, user_id = ledger

        async with session.begin():
            await set_tenant(session, tenant_id)
            txn_id = await create_transaction(session, tenant_id, account_id)

        with pytest.raises(DBAPIError):
            async with session.begin():
                await set_tenant(session, tenant_id)
                await session.execute(
                    text("UPDATE transactions SET amount = 1 WHERE id = :id"),
                    {"id": txn_id},
                )


class TestVerifiedCorrectionProtection:
    async def test_ai_cannot_modify_a_verified_transaction(self, session, ledger) -> None:
        """The guarantee that a user's correction is permanent."""
        tenant_id, account_id, user_id = ledger

        async with session.begin():
            await set_tenant(session, tenant_id)
            txn_id = await create_transaction(session, tenant_id, account_id)
            await verify_transaction(session, txn_id, user_id)

        with pytest.raises(DBAPIError) as excinfo:
            async with session.begin():
                await set_tenant(session, tenant_id)
                await set_actor(session, ActorKind.AI)
                await session.execute(
                    text(
                        "UPDATE transactions SET category_source = 'ai_model' "
                        "WHERE id = :id"
                    ),
                    {"id": txn_id},
                )

        assert "verified" in str(excinfo.value).lower()

    async def test_a_user_can_still_modify_their_own_verified_transaction(
        self, session, ledger
    ) -> None:
        """Protection is against the AI, not against the person."""
        tenant_id, account_id, user_id = ledger

        async with session.begin():
            await set_tenant(session, tenant_id)
            txn_id = await create_transaction(session, tenant_id, account_id)
            await verify_transaction(session, txn_id, user_id)

        async with session.begin():
            await set_tenant(session, tenant_id)
            await set_actor(session, ActorKind.USER)
            await session.execute(
                text(
                    "UPDATE transactions SET corrected_merchant = 'Corrected By Human' "
                    "WHERE id = :id"
                ),
                {"id": txn_id},
            )

        async with session.begin():
            await set_tenant(session, tenant_id)
            merchant = await session.scalar(
                text("SELECT merchant FROM transactions WHERE id = :id"), {"id": txn_id}
            )

        assert merchant == "Corrected By Human"

    async def test_ai_may_modify_an_unverified_transaction(self, session, ledger) -> None:
        tenant_id, account_id, user_id = ledger

        async with session.begin():
            await set_tenant(session, tenant_id)
            txn_id = await create_transaction(session, tenant_id, account_id)

        async with session.begin():
            await set_tenant(session, tenant_id)
            await set_actor(session, ActorKind.AI)
            await session.execute(
                text("UPDATE transactions SET category_source = 'ai_model' WHERE id = :id"),
                {"id": txn_id},
            )

        async with session.begin():
            await set_tenant(session, tenant_id)
            source = await session.scalar(
                text("SELECT category_source FROM transactions WHERE id = :id"),
                {"id": txn_id},
            )
        assert source == "ai_model"

    async def test_a_verified_row_must_record_who_verified_it(
        self, session, ledger
    ) -> None:
        tenant_id, account_id, user_id = ledger

        with pytest.raises(IntegrityError):
            async with session.begin():
                await set_tenant(session, tenant_id)
                await create_transaction(
                    session, tenant_id, account_id, is_verified=True
                )


class TestDuplicatePrevention:
    async def test_the_database_refuses_a_duplicate_fingerprint(
        self, session, ledger
    ) -> None:
        """Re-uploading an overlapping statement must not double-count.

        Enforced by a unique constraint, so it holds even if the deduplication
        pass is skipped, buggy, or racing another worker.
        """
        tenant_id, account_id, user_id = ledger
        fingerprint = uuid.uuid4().hex * 2

        async with session.begin():
            await set_tenant(session, tenant_id)
            await create_transaction(
                session, tenant_id, account_id, fingerprint=fingerprint
            )

        with pytest.raises(IntegrityError):
            async with session.begin():
                await set_tenant(session, tenant_id)
                await create_transaction(
                    session, tenant_id, account_id, fingerprint=fingerprint
                )

    async def test_the_same_fingerprint_is_fine_for_a_different_tenant(
        self, session, ledger
    ) -> None:
        """Uniqueness is scoped per tenant and account, not globally.

        Two people can genuinely have an identical ₹99 Netflix charge on the
        same day, and neither should block the other's import.
        """
        tenant_id, account_id, user_id = ledger
        fingerprint = uuid.uuid4().hex * 2

        async with session.begin():
            await set_tenant(session, tenant_id)
            await create_transaction(
                session, tenant_id, account_id, fingerprint=fingerprint
            )

        async with session.begin():
            other_tenant = await create_tenant(session, "Other")
            other_account = await create_account(session, other_tenant, "3333")
            await create_transaction(
                session, other_tenant, other_account, fingerprint=fingerprint
            )

        async with session.begin():
            await set_tenant(session, other_tenant)
            await session.execute(
                text("DELETE FROM transactions WHERE tenant_id = :t"), {"t": other_tenant}
            )
            await session.execute(
                text("DELETE FROM accounts WHERE tenant_id = :t"), {"t": other_tenant}
            )
            await session.execute(
                text("DELETE FROM tenants WHERE id = :t"), {"t": other_tenant}
            )


class TestAppendOnlyAudit:
    async def test_audit_entries_cannot_be_updated_or_deleted(
        self, session, ledger
    ) -> None:
        """An audit trail that can be edited is not an audit trail."""
        tenant_id, account_id, user_id = ledger

        async with session.begin():
            await set_tenant(session, tenant_id)
            txn_id = await create_transaction(session, tenant_id, account_id)
            audit_id = uuid.uuid4()
            await session.execute(
                text(
                    """
                    INSERT INTO transaction_audit (
                        id, tenant_id, transaction_id, actor_kind,
                        field_name, old_value, new_value, changed_at
                    ) VALUES (
                        :id, :tenant_id, :txn_id, 'user',
                        'category_id', 'a', 'b', now()
                    )
                    """
                ),
                {"id": audit_id, "tenant_id": tenant_id, "txn_id": txn_id},
            )

        # An audit entry can never be rewritten. No exceptions, no escape hatch.
        with pytest.raises(DBAPIError) as excinfo:
            async with session.begin():
                await set_tenant(session, tenant_id)
                await session.execute(
                    text("UPDATE transaction_audit SET new_value = 'c' WHERE id = :id"),
                    {"id": audit_id},
                )
        assert "append-only" in str(excinfo.value).lower()

        # Nor deleted by accident.
        with pytest.raises(DBAPIError) as excinfo:
            async with session.begin():
                await set_tenant(session, tenant_id)
                await session.execute(
                    text("DELETE FROM transaction_audit WHERE id = :id"), {"id": audit_id}
                )
        assert "append-only" in str(excinfo.value).lower()

    async def test_audit_entries_can_be_erased_when_explicitly_permitted(
        self, session, ledger
    ) -> None:
        """Right to erasure still works.

        Blocking DELETE outright would mean an audit trail that pins data a user
        has asked to have removed — and would break ON DELETE CASCADE from the
        parent transaction. Deletion is allowed, but only when the caller opts
        in for that transaction.
        """
        tenant_id, account_id, user_id = ledger

        async with session.begin():
            await set_tenant(session, tenant_id)
            txn_id = await create_transaction(session, tenant_id, account_id)
            audit_id = uuid.uuid4()
            await session.execute(
                text(
                    """
                    INSERT INTO transaction_audit (
                        id, tenant_id, transaction_id, actor_kind,
                        field_name, old_value, new_value, changed_at
                    ) VALUES (
                        :id, :tenant_id, :txn_id, 'user',
                        'category_id', 'a', 'b', now()
                    )
                    """
                ),
                {"id": audit_id, "tenant_id": tenant_id, "txn_id": txn_id},
            )

        async with session.begin():
            await set_tenant(session, tenant_id)
            await allow_audit_purge(session)
            await session.execute(
                text("DELETE FROM transaction_audit WHERE id = :id"), {"id": audit_id}
            )

        async with session.begin():
            await set_tenant(session, tenant_id)
            remaining = await session.scalar(
                text("SELECT count(*) FROM transaction_audit WHERE id = :id"),
                {"id": audit_id},
            )
        assert remaining == 0


class TestMoneyPrecision:
    async def test_money_survives_a_value_that_would_break_a_float(
        self, session, ledger
    ) -> None:
        """0.1 + 0.2 != 0.3 in binary floating point.

        NUMERIC makes this boring, which is the entire objective.
        """
        tenant_id, account_id, user_id = ledger

        async with session.begin():
            await set_tenant(session, tenant_id)
            first = await create_transaction(session, tenant_id, account_id, amount="0.10")
            second = await create_transaction(session, tenant_id, account_id, amount="0.20")

            total = await session.scalar(
                text(
                    "SELECT SUM(amount) FROM transactions WHERE id IN (:a, :b)"
                ),
                {"a": first, "b": second},
            )

        assert total == Decimal("0.30")
        assert str(total) == "0.30"

    async def test_a_large_indian_amount_keeps_full_precision(
        self, session, ledger
    ) -> None:
        tenant_id, account_id, user_id = ledger

        async with session.begin():
            await set_tenant(session, tenant_id)
            txn_id = await create_transaction(
                session, tenant_id, account_id, amount="12345678.99"
            )
            amount = await session.scalar(
                text("SELECT amount FROM transactions WHERE id = :id"), {"id": txn_id}
            )

        assert amount == Decimal("12345678.99")

    async def test_a_negative_amount_is_rejected(self, session, ledger) -> None:
        """Sign lives in `direction`. A negative amount would mean two encodings
        of the same fact, and eventually they would disagree."""
        tenant_id, account_id, user_id = ledger

        with pytest.raises(IntegrityError):
            async with session.begin():
                await set_tenant(session, tenant_id)
                await create_transaction(
                    session, tenant_id, account_id, amount="-100.00"
                )
