"""Retention, erasure, and the guards on the job that deletes things.

The tests in the last class exist because of a real incident during this
phase. ``reconcile_objects`` built its set of live objects by enumerating
tenants on an unscoped session; Row Level Security is enabled on ``tenants``,
so it enumerated none, concluded the whole bucket was orphaned, and deleted it.

The query bug was one line. What actually needed fixing was a job that treated
"I found no tenants" as a valid basis for deletion — so the guards are tested
individually, and each one is asserted to be sufficient on its own. A
destructive operation protected by a single check is one bug away from the same
outcome.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.db.session import scoped_session
from app.services import retention

from tests.conftest import register_user


@pytest.fixture
async def tenant(client) -> uuid.UUID:
    user = await register_user(client)
    return uuid.UUID(user["user"]["tenant_id"])


async def _statement(
    tenant_id: uuid.UUID, *, deleted_days_ago: int | None = None
) -> uuid.UUID:
    statement_id = uuid.uuid4()
    async with scoped_session(tenant_id, actor="system") as session:
        await session.execute(
            text(
                """
                INSERT INTO statements (
                    id, tenant_id, storage_key, file_size_bytes, file_sha256,
                    document_type, status, trust_status, page_count, deleted_at
                ) VALUES (
                    :id, :tenant_id, :key, 1000, :digest,
                    'unknown', 'processed', 'pending', 1, :deleted_at
                )
                """
            ),
            {
                "id": statement_id,
                "tenant_id": tenant_id,
                "key": f"tenants/{tenant_id}/statements/{statement_id}.pdf",
                "digest": uuid.uuid4().hex * 2,
                "deleted_at": (
                    datetime.now(timezone.utc) - timedelta(days=deleted_days_ago)
                    if deleted_days_ago is not None else None
                ),
            },
        )
    return statement_id


class TestTheSweepIsConservative:
    async def test_a_live_statement_is_never_removed(self, tenant, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "STATEMENT_RETENTION_DAYS", 1)
        await _statement(tenant)

        async with scoped_session(tenant, actor="system") as session:
            result = await retention.sweep(session)
            remaining = (
                await session.execute(text("SELECT count(*) FROM statements"))
            ).scalar_one()

        assert remaining == 1
        assert "statements" not in result.deleted

    async def test_a_soft_deleted_statement_past_its_window_goes(
        self, tenant, monkeypatch
    ):
        """Both conditions, not either: the user deleted it *and* time passed."""
        from app.core.config import settings

        monkeypatch.setattr(settings, "STATEMENT_RETENTION_DAYS", 30)
        await _statement(tenant, deleted_days_ago=60)

        async with scoped_session(tenant, actor="system") as session:
            result = await retention.sweep(session)
            remaining = (
                await session.execute(text("SELECT count(*) FROM statements"))
            ).scalar_one()

        assert remaining == 0
        assert result.deleted["statements"] == 1

    async def test_a_recently_deleted_statement_stays(self, tenant, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "STATEMENT_RETENTION_DAYS", 30)
        await _statement(tenant, deleted_days_ago=5)

        async with scoped_session(tenant, actor="system") as session:
            await retention.sweep(session)
            remaining = (
                await session.execute(text("SELECT count(*) FROM statements"))
            ).scalar_one()
        assert remaining == 1

    async def test_the_sweep_reports_the_keys_it_orphaned(self, tenant, monkeypatch):
        """The whole reason object cleanup can be precise instead of a diff."""
        from app.core.config import settings

        monkeypatch.setattr(settings, "STATEMENT_RETENTION_DAYS", 30)
        statement_id = await _statement(tenant, deleted_days_ago=60)

        async with scoped_session(tenant, actor="system") as session:
            result = await retention.sweep(session)

        assert len(result.orphaned_keys) == 1
        assert str(statement_id) in result.orphaned_keys[0]

    async def test_a_transaction_is_never_in_scope(self, tenant, monkeypatch):
        """No retention window applies to the ledger, and none may be added
        without this test failing."""
        from app.core.config import settings

        monkeypatch.setattr(settings, "STATEMENT_RETENTION_DAYS", 0)
        monkeypatch.setattr(settings, "AUDIT_LOG_RETENTION_DAYS", 0)
        monkeypatch.setattr(settings, "JOB_EVENT_RETENTION_DAYS", 0)

        async with scoped_session(tenant, actor="system") as session:
            result = await retention.sweep(session)

        assert "transactions" not in result.deleted


class TestErasure:
    async def test_everything_belonging_to_the_tenant_goes(self, tenant, client):
        from tests.conftest import create_account, create_transaction, set_tenant

        async with scoped_session(tenant, actor="system") as session:
            await set_tenant(session, tenant)
            account_id = await create_account(session, tenant)
            await create_transaction(session, tenant, account_id)
            await _statement(tenant)

        async with scoped_session(tenant, actor="system") as session:
            counts = await retention.erase_tenant(session, tenant_id=tenant)

        assert counts["transactions"] >= 1

        # Read back as the owner: a scoped read after the tenant is gone would
        # return nothing whether or not the rows survived, which would make
        # this assertion meaningless.
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy.pool import NullPool

        from app.core.config import settings

        owner_url = settings.database_url.replace(
            f"{settings.APP_DB_USER}:{settings.APP_DB_PASSWORD}",
            f"{settings.POSTGRES_USER}:{settings.POSTGRES_PASSWORD}",
        )
        engine = create_async_engine(
            owner_url, poolclass=NullPool, connect_args={"statement_cache_size": 0}
        )
        try:
            async with async_sessionmaker(bind=engine)() as session:
                for table in ("transactions", "statements", "accounts", "users", "tenants"):
                    column = "id" if table == "tenants" else "tenant_id"
                    count = (
                        await session.execute(
                            text(f"SELECT count(*) FROM {table} WHERE {column} = :t"),
                            {"t": tenant},
                        )
                    ).scalar_one()
                    assert count == 0, f"{table} still has rows"
        finally:
            await engine.dispose()

    async def test_the_audit_trail_goes_too(self, tenant):
        """Erasure means erasure. The append-only guard permits it only when
        the purge flag is set, and `erase_tenant` is what sets it."""
        from app.models.enums import AuditAction
        from app.services import audit

        async with scoped_session(tenant, actor="system") as session:
            await audit.record(
                session, tenant_id=tenant, action=AuditAction.LOGIN,
            )

        async with scoped_session(tenant, actor="system") as session:
            counts = await retention.erase_tenant(session, tenant_id=tenant)

        assert counts["audit_logs"] >= 1

    async def test_an_audit_row_cannot_be_deleted_without_the_flag(self, tenant):
        """The guard itself, so the test above is not passing by accident."""
        from app.models.enums import AuditAction
        from app.services import audit

        async with scoped_session(tenant, actor="system") as session:
            await audit.record(session, tenant_id=tenant, action=AuditAction.LOGIN)

        with pytest.raises(Exception) as raised:
            async with scoped_session(tenant, actor="system") as session:
                await session.execute(text("DELETE FROM audit_logs"))
        assert "append-only" in str(raised.value) or "insufficient_privilege" in str(
            raised.value
        )


class TestTheReconciliationGuards:
    """Four independent refusals. Each is asserted to be sufficient alone."""

    async def test_enumerating_no_tenants_refuses(self, monkeypatch):
        from workers.tasks import maintenance

        async def _none() -> list[uuid.UUID]:
            return []

        monkeypatch.setattr(maintenance, "_tenants", _none)
        report = await maintenance._reconcile(delete=True)

        assert report["refused"] == "no_tenants_enumerated"
        assert report["deleted"] == 0

    async def test_an_empty_live_set_against_a_full_bucket_refuses(
        self, tenant, monkeypatch
    ):
        """The exact shape of the incident this guard was written for."""
        from workers.tasks import maintenance

        async def _one() -> list[uuid.UUID]:
            return [tenant]

        async def _no_keys(session) -> list[str]:
            return []

        class _Item:
            def __init__(self, name: str) -> None:
                self.object_name = name

        class _Storage:
            removed: list[str] = []

            def list_objects(self, *_args, **_kwargs):
                return [_Item(f"tenants/x/statements/{index}.pdf") for index in range(10)]

            def remove_object(self, _bucket, name):
                self.removed.append(name)

        storage = _Storage()
        monkeypatch.setattr(maintenance, "_tenants", _one)
        monkeypatch.setattr(retention, "storage_keys", _no_keys)
        monkeypatch.setattr("app.core.storage.get_storage", lambda: storage)

        report = await maintenance._reconcile(delete=True)

        assert report["refused"] == "no_referenced_objects"
        assert report["deleted"] == 0
        assert storage.removed == []

    async def test_too_large_a_fraction_refuses(self, tenant, monkeypatch):
        from workers.tasks import maintenance

        async def _one() -> list[uuid.UUID]:
            return [tenant]

        async def _one_key(session) -> list[str]:
            return ["tenants/x/statements/0.pdf"]

        class _Item:
            def __init__(self, name: str) -> None:
                self.object_name = name

        class _Storage:
            removed: list[str] = []

            def list_objects(self, *_args, **_kwargs):
                return [_Item(f"tenants/x/statements/{index}.pdf") for index in range(10)]

            def remove_object(self, _bucket, name):
                self.removed.append(name)

        storage = _Storage()
        monkeypatch.setattr(maintenance, "_tenants", _one)
        monkeypatch.setattr(retention, "storage_keys", _one_key)
        monkeypatch.setattr("app.core.storage.get_storage", lambda: storage)

        report = await maintenance._reconcile(delete=True)

        assert report["refused"] == "orphan_fraction_too_high"
        assert storage.removed == []

    async def test_it_reports_without_deleting_by_default(self, tenant, monkeypatch):
        """The scheduled run measures. Deleting is a decision someone makes."""
        from workers.tasks import maintenance

        async def _one() -> list[uuid.UUID]:
            return [tenant]

        async def _keys(session) -> list[str]:
            return [f"tenants/x/statements/{index}.pdf" for index in range(9)]

        class _Item:
            def __init__(self, name: str) -> None:
                self.object_name = name

        class _Storage:
            removed: list[str] = []

            def list_objects(self, *_args, **_kwargs):
                return [_Item(f"tenants/x/statements/{index}.pdf") for index in range(10)]

            def remove_object(self, _bucket, name):
                self.removed.append(name)

        storage = _Storage()
        monkeypatch.setattr(maintenance, "_tenants", _one)
        monkeypatch.setattr(retention, "storage_keys", _keys)
        monkeypatch.setattr("app.core.storage.get_storage", lambda: storage)

        report = await maintenance._reconcile(delete=False)

        assert report["refused"] is None
        assert report["orphans"] == 1
        assert report["deleted"] == 0
        assert storage.removed == []

    async def test_the_tenant_lookup_does_not_silently_return_empty(self):
        """The root cause, asserted directly.

        A plain `SELECT id FROM tenants` on an unscoped session returns zero
        rows under RLS. `ops_active_tenants()` is a SECURITY DEFINER function
        precisely so that an empty answer means there are no tenants.
        """
        from workers.tasks import maintenance

        tenants = await maintenance._tenants()
        assert tenants, "ops_active_tenants() returned nothing; the guard is load-bearing"


class TestTheSeedIsIdempotent:
    """`make bootstrap` is the documented way in, and people re-run it.

    The demo seed checked for its own tenant with a plain SELECT against a
    table under Row Level Security, so the check returned "absent" every time
    and only agreed with reality on an empty database. Re-running bootstrap
    hit a unique-constraint violation — a seed documented as idempotent that
    was not. Same shape as the reconciliation bug above: an RLS-scoped read
    returning nothing, treated as proof of absence.
    """

    async def test_the_slug_lookup_crosses_the_rls_boundary(self, client):
        """A plain SELECT sees nothing; the function sees the truth."""
        from app.db.session import get_session_factory

        factory = get_session_factory()
        async with factory() as session:
            plain = (
                await session.execute(
                    text("SELECT id FROM tenants WHERE slug = 'demo'")
                )
            ).scalar_one_or_none()
            via_function = (
                await session.execute(
                    text("SELECT ops_tenant_id_by_slug('demo')")
                )
            ).scalar_one_or_none()

        assert plain is None, "RLS should hide it from an unscoped SELECT"
        if via_function is None:
            pytest.skip("no demo tenant in this database; run `make seed-demo`")
        assert via_function is not None

    async def test_seeding_the_demo_tenant_twice_changes_nothing(self, client):
        from app.db.seed import seed_demo_tenant
        from app.db.session import get_session_factory

        factory = get_session_factory()
        async with factory() as session:
            first = await seed_demo_tenant(session)
            await session.commit()
        async with factory() as session:
            second = await seed_demo_tenant(session)
            await session.commit()

        assert first is not None
        assert first == second, "the second run created a new tenant"

    async def test_an_absent_slug_returns_null_rather_than_raising(self, client):
        from app.db.session import get_session_factory

        factory = get_session_factory()
        async with factory() as session:
            result = (
                await session.execute(
                    text("SELECT ops_tenant_id_by_slug('no-such-tenant')")
                )
            ).scalar_one_or_none()
        assert result is None


class TestTheSeedAndThePipelineAgree:
    """A seeded account must be the account an import resolves to.

    The seed computed `account_fingerprint` as a `uuid5` while the pipeline
    used an HMAC of the same inputs — two schemes for one concept. Nothing
    failed: the import simply created its own account beside the seeded one,
    and every demo carried two empty placeholders that could never receive a
    transaction.
    """

    async def test_the_seed_uses_the_pipelines_own_fingerprint(self):
        from app.core.security import account_fingerprint
        from app.db.session import get_session_factory

        factory = get_session_factory()
        async with factory() as session:
            tenant_id = (
                await session.execute(text("SELECT ops_tenant_id_by_slug('demo')"))
            ).scalar_one_or_none()
        if tenant_id is None:
            pytest.skip("no demo tenant; run `make seed-demo`")

        async with scoped_session(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT bank_code, account_type, account_last4, "
                        "account_fingerprint FROM accounts"
                    )
                )
            ).all()

        assert rows, "the demo tenant has no accounts"
        for row in rows:
            assert row.account_fingerprint == account_fingerprint(
                tenant_id, row.bank_code, row.account_type, row.account_last4
            ), f"{row.bank_code} ••••{row.account_last4} uses a different scheme"

    async def test_the_demo_has_no_account_without_transactions(self):
        """Two accounts, both used. An empty one means the schemes drifted."""
        from app.db.session import get_session_factory

        factory = get_session_factory()
        async with factory() as session:
            tenant_id = (
                await session.execute(text("SELECT ops_tenant_id_by_slug('demo')"))
            ).scalar_one_or_none()
        if tenant_id is None:
            pytest.skip("no demo tenant; run `make seed-demo`")

        async with scoped_session(tenant_id) as session:
            empty = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM accounts a WHERE NOT EXISTS "
                        "(SELECT 1 FROM transactions t WHERE t.account_id = a.id)"
                    )
                )
            ).scalar_one()

        # Only meaningful once statements have been imported.
        async with scoped_session(tenant_id) as session:
            imported = (
                await session.execute(text("SELECT count(*) FROM transactions"))
            ).scalar_one()
        if not imported:
            pytest.skip("no demo transactions; run `make demo-data`")

        assert empty == 0, f"{empty} demo accounts have no transactions"
