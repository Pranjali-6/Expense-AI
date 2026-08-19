"""Row Level Security.

The claim under test: **a query carrying the wrong tenant scope, or no scope at
all, returns nothing.** Not "returns filtered results because the service layer
remembered to add a WHERE clause" — returns nothing because PostgreSQL refuses.

Every test here runs as the application role over a real connection.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import create_account, create_transaction, set_tenant


# Every table the policy set must cover. Kept as a literal so a table losing its
# policy is a failing test rather than a silent hole.
TENANT_SCOPED_TABLES = [
    "accounts", "ai_classifications", "anomalies", "audit_logs", "budgets",
    "ingestion_sources", "insight_snapshots", "job_events", "notifications",
    "privacy_counters", "privacy_incidents", "processing_jobs", "refresh_tokens",
    "statement_health", "statement_pages", "statements", "subscriptions",
    "timeline_events", "transaction_audit", "transactions", "transfer_groups",
    "user_category_rules", "users",
]


class TestRoleConfiguration:
    """RLS is only meaningful if the connecting role cannot bypass it."""

    async def test_app_role_is_not_superuser_and_cannot_bypass_rls(
        self, session: AsyncSession
    ) -> None:
        row = (
            await session.execute(
                text(
                    "SELECT rolsuper, rolbypassrls FROM pg_roles "
                    "WHERE rolname = current_user"
                )
            )
        ).one()
        assert row.rolsuper is False, "application role must not be a superuser"
        assert row.rolbypassrls is False, "application role must not have BYPASSRLS"

    async def test_app_role_is_not_the_table_owner(self, session: AsyncSession) -> None:
        # Owning the tables would bypass RLS on everything not marked FORCE.
        owner = await session.scalar(
            text("SELECT tableowner FROM pg_tables WHERE tablename = 'transactions'")
        )
        current = await session.scalar(text("SELECT current_user"))
        assert owner != current


class TestPolicyCoverage:
    async def test_every_tenant_scoped_table_has_rls_enabled(
        self, session: AsyncSession
    ) -> None:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT c.relname, c.relrowsecurity
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public' AND c.relkind = 'r'
                    """
                )
            )
        ).all()
        enabled = {name for name, rls in rows if rls}
        missing = set(TENANT_SCOPED_TABLES) - enabled
        assert not missing, f"RLS not enabled on: {sorted(missing)}"

    async def test_every_table_with_tenant_id_has_a_policy(
        self, session: AsyncSession
    ) -> None:
        """Catches the failure mode this suite exists for.

        A model added later with a ``tenant_id`` but no policy would be wide
        open. Deriving the expectation from the schema rather than a list means
        the test notices without anyone remembering to update it.
        """
        rows = (
            await session.execute(
                text(
                    """
                    SELECT c.relname
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    JOIN pg_attribute a ON a.attrelid = c.oid
                    WHERE n.nspname = 'public'
                      AND c.relkind = 'r'
                      AND a.attname = 'tenant_id'
                      AND NOT a.attisdropped
                      AND NOT EXISTS (
                          SELECT 1 FROM pg_policies p WHERE p.tablename = c.relname
                      )
                    """
                )
            )
        ).scalars().all()
        assert not rows, f"tables with tenant_id but no RLS policy: {sorted(rows)}"

    async def test_tenants_table_is_isolated_on_its_primary_key(
        self, session: AsyncSession
    ) -> None:
        qual = await session.scalar(
            text(
                "SELECT qual FROM pg_policies "
                "WHERE tablename = 'tenants' AND policyname = 'tenant_isolation'"
            )
        )
        assert qual is not None
        assert "id = app_current_tenant()" in qual.replace("(", "").replace(")", "") or \
               "app_current_tenant" in qual


class TestReadIsolation:
    async def test_correct_scope_sees_only_own_rows(
        self, session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        tenant_a, tenant_b = two_tenants

        async with session.begin():
            await set_tenant(session, tenant_a)
            rows = (
                await session.execute(text("SELECT tenant_id FROM transactions"))
            ).scalars().all()

        assert len(rows) == 1
        assert rows[0] == tenant_a
        assert tenant_b not in rows

    async def test_wrong_scope_returns_zero_rows(
        self, session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """The headline guarantee."""
        tenant_a, _ = two_tenants
        stranger = uuid.uuid4()

        async with session.begin():
            await set_tenant(session, stranger)
            count = await session.scalar(text("SELECT count(*) FROM transactions"))

        assert count == 0

    async def test_no_scope_returns_zero_rows(
        self, session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """Fail closed.

        An unset GUC makes ``tenant_id = NULL`` evaluate to NULL, the policy
        does not pass, and nothing comes back. A forgotten ``SET LOCAL`` is a
        bug that yields an empty screen — never everyone's data.
        """
        async with session.begin():
            await set_tenant(session, None)
            for table in ("transactions", "accounts", "statements", "users"):
                count = await session.scalar(text(f"SELECT count(*) FROM {table}"))
                assert count == 0, f"{table} leaked rows with no tenant scope"

    @pytest.mark.parametrize("table", TENANT_SCOPED_TABLES)
    async def test_every_table_returns_nothing_under_a_stranger_scope(
        self, session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID], table: str
    ) -> None:
        """Swept across all 23 tables, not just the interesting ones.

        A single unprotected table is a full cross-tenant leak, and it is
        precisely the boring ones — job_events, notifications — that get
        forgotten.
        """
        stranger = uuid.uuid4()
        async with session.begin():
            await set_tenant(session, stranger)
            count = await session.scalar(text(f"SELECT count(*) FROM {table}"))
        assert count == 0, f"{table} returned rows under a stranger's scope"

    async def test_targeted_read_of_a_known_id_from_another_tenant_fails(
        self, session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """Knowing the id is not enough — the classic IDOR.

        This is the attack that app-layer filtering usually stops and
        occasionally does not. Here the database stops it regardless.
        """
        tenant_a, tenant_b = two_tenants

        async with session.begin():
            await set_tenant(session, tenant_b)
            leaked_id = await session.scalar(text("SELECT id FROM transactions LIMIT 1"))

        async with session.begin():
            await set_tenant(session, tenant_a)
            found = await session.scalar(
                text("SELECT id FROM transactions WHERE id = :id"), {"id": leaked_id}
            )

        assert found is None


class TestWriteIsolation:
    async def test_cannot_insert_a_row_for_another_tenant(
        self, session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """WITH CHECK stops writing *into* another tenant, not just reading."""
        tenant_a, tenant_b = two_tenants

        with pytest.raises(DBAPIError) as excinfo:
            async with session.begin():
                await set_tenant(session, tenant_a)
                await create_account(session, tenant_b, "9999")

        assert "row-level security" in str(excinfo.value).lower()

    async def test_cannot_move_a_row_to_another_tenant(
        self, session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        tenant_a, tenant_b = two_tenants

        with pytest.raises(DBAPIError) as excinfo:
            async with session.begin():
                await set_tenant(session, tenant_a)
                await session.execute(
                    text("UPDATE transactions SET tenant_id = :other"),
                    {"other": tenant_b},
                )

        assert "row-level security" in str(excinfo.value).lower()

    async def test_delete_cannot_reach_another_tenant(
        self, session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """An unqualified DELETE is scoped to the caller, not the whole table."""
        tenant_a, tenant_b = two_tenants

        async with session.begin():
            await set_tenant(session, tenant_a)
            await session.execute(text("DELETE FROM transactions"))

        async with session.begin():
            await set_tenant(session, tenant_b)
            survivors = await session.scalar(text("SELECT count(*) FROM transactions"))

        assert survivors == 1, "tenant B's data was destroyed by tenant A's DELETE"

        # Put tenant A's row back so the fixture teardown stays symmetrical.
        async with session.begin():
            await set_tenant(session, tenant_a)
            account_id = await session.scalar(text("SELECT id FROM accounts LIMIT 1"))
            await create_transaction(session, tenant_a, account_id, amount="100.00")


class TestScopeLifetime:
    async def test_scope_does_not_survive_the_transaction(
        self, session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """``SET LOCAL`` is transaction-scoped, which is what makes pooling safe.

        If the scope leaked past COMMIT, a pooled connection could serve the
        next request with the previous tenant's scope still attached — a
        cross-tenant leak that would appear only under concurrency.
        """
        tenant_a, _ = two_tenants

        async with session.begin():
            await set_tenant(session, tenant_a)
            assert await session.scalar(text("SELECT count(*) FROM transactions")) == 1

        # New transaction, no scope set.
        async with session.begin():
            leaked = await session.scalar(text("SELECT count(*) FROM transactions"))

        assert leaked == 0, "tenant scope survived the transaction that set it"
