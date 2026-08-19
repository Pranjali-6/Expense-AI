"""Shared test fixtures.

These tests run against the **real PostgreSQL instance**, connecting as the real
application role. That is deliberate and not negotiable for this suite: Row
Level Security, generated columns and triggers are database behaviour, and a
SQLite or mocked-session test would happily pass while every one of those
protections was broken in production.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import httpx

from app.core.config import settings
from app.core.redis_client import close_redis
from app.db.session import dispose_engine
from app.models.enums import ActorKind


@pytest_asyncio.fixture(scope="function")
async def engine():
    """A dedicated engine per test, with pooling disabled.

    The application's engine is a module-level singleton with a real pool, and
    asyncpg binds each connection to the event loop that opened it. pytest-asyncio
    creates a fresh loop per test, so reusing that pool makes the second test
    fail with "attached to a different loop" — a property of the test harness,
    not of the code under test. NullPool sidesteps it: every test opens and
    closes its own connections inside its own loop.
    """
    test_engine = create_async_engine(
        settings.database_url,
        poolclass=NullPool,
        connect_args={"statement_cache_size": 0},
    )
    try:
        yield test_engine
    finally:
        await test_engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def session(engine) -> AsyncIterator[AsyncSession]:
    """An unscoped session with no tenant GUC set.

    Starts with no scope on purpose: a test that forgets to set one should see
    the fail-closed behaviour rather than a convenient default.
    """
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    async with factory() as db:
        yield db
        await db.rollback()


async def set_tenant(db: AsyncSession, tenant_id: uuid.UUID | str | None) -> None:
    """Bind the RLS tenant scope for the current transaction."""
    await db.execute(
        text("SELECT set_config('app.current_tenant_id', :tid, true)"),
        {"tid": str(tenant_id) if tenant_id else ""},
    )


async def set_actor(db: AsyncSession, actor: ActorKind | str) -> None:
    """Declare who is performing writes, for the verified-row trigger."""
    await db.execute(
        text("SELECT set_config('app.actor_kind', :actor, true)"),
        {"actor": str(actor)},
    )


async def allow_audit_purge(db: AsyncSession) -> None:
    """Opt in to deleting audit rows for the current transaction.

    Mirrors what the account-deletion path does. Without it, audit rows refuse
    to be deleted — which is the point.
    """
    await db.execute(
        text("SELECT set_config('app.allow_audit_purge', 'on', true)")
    )


async def create_tenant(db: AsyncSession, name: str) -> uuid.UUID:
    """Create a tenant, scoping the session to it first.

    The policy's WITH CHECK compares the new row's id to the GUC, so the scope
    has to exist before the row does.
    """
    tenant_id = uuid.uuid4()
    slug = f"{name}-{tenant_id.hex[:8]}"
    await set_tenant(db, tenant_id)
    await db.execute(
        text(
            "INSERT INTO tenants (id, name, slug, ai_enabled) "
            "VALUES (:id, :name, :slug, true)"
        ),
        {"id": tenant_id, "name": name, "slug": slug},
    )
    return tenant_id


async def create_user(db: AsyncSession, tenant_id: uuid.UUID) -> uuid.UUID:
    user_id = uuid.uuid4()
    await db.execute(
        text(
            """
            INSERT INTO users (
                id, tenant_id, email, full_name, password_hash,
                auth_provider, role, status
            ) VALUES (
                :id, :tenant_id, :email, 'Test User', 'x',
                'password', 'owner', 'active'
            )
            """
        ),
        {"id": user_id, "tenant_id": tenant_id, "email": f"user-{user_id.hex[:12]}@example.com"},
    )
    return user_id


async def create_account(db: AsyncSession, tenant_id: uuid.UUID, last4: str = "1234") -> uuid.UUID:
    account_id = uuid.uuid4()
    await db.execute(
        text(
            """
            INSERT INTO accounts (
                id, tenant_id, bank_code, bank_name, account_type, status,
                account_last4, account_fingerprint
            ) VALUES (
                :id, :tenant_id, 'HDFC', 'HDFC Bank', 'savings', 'active',
                :last4, :fingerprint
            )
            """
        ),
        {
            "id": account_id,
            "tenant_id": tenant_id,
            "last4": last4,
            "fingerprint": uuid.uuid4().hex,
        },
    )
    return account_id


async def create_transaction(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    amount: str = "450.00",
    description: str = "TEST MERCHANT PURCHASE",
    direction: str = "debit",
    txn_date: date | None = None,
    fingerprint: str | None = None,
    confidences: tuple[str, str, str, str] = ("0.990", "0.980", "0.950", "1.000"),
    is_verified: bool = False,
) -> uuid.UUID:
    transaction_id = uuid.uuid4()
    await db.execute(
        text(
            """
            INSERT INTO transactions (
                id, tenant_id, account_id,
                original_txn_date, original_description, original_amount,
                original_direction, original_payment_method,
                category_source, movement_type, is_expense,
                confidence_extraction, confidence_merchant,
                confidence_category, confidence_validation,
                review_status, is_verified, fingerprint
            ) VALUES (
                :id, :tenant_id, :account_id,
                :txn_date, :description, :amount,
                :direction, 'unknown',
                'deterministic_rule', 'expense', true,
                :c_ext, :c_mer, :c_cat, :c_val,
                'auto_approved', :is_verified, :fingerprint
            )
            """
        ),
        {
            "id": transaction_id,
            "tenant_id": tenant_id,
            "account_id": account_id,
            "txn_date": txn_date or date(2026, 3, 15),
            "description": description,
            "amount": Decimal(amount),
            "direction": direction,
            "c_ext": Decimal(confidences[0]),
            "c_mer": Decimal(confidences[1]),
            "c_cat": Decimal(confidences[2]),
            "c_val": Decimal(confidences[3]),
            "is_verified": is_verified,
            "fingerprint": fingerprint or uuid.uuid4().hex + uuid.uuid4().hex[:32],
        },
    )
    return transaction_id


async def verify_transaction(
    db: AsyncSession, transaction_id: uuid.UUID, user_id: uuid.UUID | None = None
) -> None:
    """Mark a transaction human-verified, satisfying the verified_has_actor check."""
    await db.execute(
        text(
            "UPDATE transactions SET is_verified = true, verified_by = :user_id, "
            "verified_at = :now WHERE id = :id"
        ),
        {"id": transaction_id, "user_id": user_id, "now": datetime.now(timezone.utc)},
    )


@pytest_asyncio.fixture(scope="function")
async def two_tenants(session: AsyncSession) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    """Two tenants, each with an account and one transaction.

    Committed rather than rolled back, because several tests need a *fresh*
    connection to prove the scope does not leak across sessions. Cleaned up
    afterwards by tenant id.
    """
    async with session.begin():
        tenant_a = await create_tenant(session, "Tenant A")
        account_a = await create_account(session, tenant_a, "1111")
        await create_transaction(
            session, tenant_a, account_a, amount="100.00", description="TENANT A COFFEE"
        )

        tenant_b = await create_tenant(session, "Tenant B")
        account_b = await create_account(session, tenant_b, "2222")
        await create_transaction(
            session, tenant_b, account_b, amount="200.00", description="TENANT B FUEL"
        )

    yield tenant_a, tenant_b

    # Teardown runs as the app role, so it needs the scope for each tenant.
    async with session.begin():
        for tenant_id in (tenant_a, tenant_b):
            await set_tenant(session, tenant_id)
            await session.execute(
                text("DELETE FROM transactions WHERE tenant_id = :t"), {"t": tenant_id}
            )
            await session.execute(
                text("DELETE FROM accounts WHERE tenant_id = :t"), {"t": tenant_id}
            )
            await session.execute(
                text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id}
            )


# --------------------------------------------------------------------------- #
# HTTP-level fixtures
# --------------------------------------------------------------------------- #

@pytest_asyncio.fixture(scope="function", autouse=True)
async def _clear_rate_limits() -> AsyncIterator[None]:
    """Clear rate-limit windows between tests.

    The limiter stays *enabled* — it is worth testing that it fires — but every
    test would otherwise share one window and the suite would throttle itself
    after the first handful of registrations.
    """
    import redis.asyncio as aioredis

    # A throwaway client, not the application singleton. redis.asyncio binds
    # connections to the loop that opened them just as asyncpg does, so reusing
    # the singleton here fails silently in every test after the first — and a
    # silently-failing cleanup means the suite throttles itself with no clue why.
    client = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        keys = [key async for key in client.scan_iter("rl:*")]
        if keys:
            await client.delete(*keys)
    finally:
        await client.aclose()
    yield


@pytest_asyncio.fixture(scope="function", autouse=True)
async def _reset_app_engine() -> AsyncIterator[None]:
    """Dispose the application's engine between tests.

    The app holds a module-level engine, and asyncpg binds connections to the
    loop that opened them. pytest-asyncio gives each test a new loop, so an
    engine built during one test is unusable in the next. Disposing after each
    test makes the app rebuild it in the right loop.
    """
    yield
    await dispose_engine()
    # Same loop-affinity problem: the app's redis client must not outlive the
    # loop it was created in, or rate limiting silently fails open in later
    # tests and the limiter appears not to work.
    await close_redis()


@pytest_asyncio.fixture(scope="function")
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client speaking to the real ASGI app, in-process.

    Exercises the genuine middleware, dependency and error-handling stack —
    including authentication and the tenant-scoping dependency — without a
    network hop.
    """
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http:
        yield http


def unique_email(prefix: str = "user") -> str:
    # `.local`, `.test` and `.example` are special-use domains that email
    # validation rejects outright, so tests use a real TLD form.
    return f"{prefix}-{uuid.uuid4().hex[:12]}@example.com"


STRONG_PASSWORD = "CorrectHorseBattery9"


async def register_user(
    http: httpx.AsyncClient, email: str | None = None, password: str = STRONG_PASSWORD
) -> dict:
    """Register and return {email, password, access_token, cookies, user}."""
    address = email or unique_email()
    response = await http.post(
        "/api/v1/auth/register",
        json={"email": address, "password": password, "full_name": "Test Person"},
    )
    response.raise_for_status()
    body = response.json()
    return {
        "email": address,
        "password": password,
        "access_token": body["access_token"],
        "user": body["user"],
        "cookies": dict(response.cookies),
    }


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
