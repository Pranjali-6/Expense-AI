"""Database engine and session management.

The engine connects as ``APP_DB_USER`` — a role created NOSUPERUSER/NOBYPASSRLS
by ``infrastructure/postgres/init/02-app-role.sh``. This is load-bearing: a
connection as the table owner would bypass every Row Level Security policy and
silently reduce tenant isolation to whatever the application code happened to
remember to filter.

Tenant scoping is applied per session via ``SET LOCAL app.current_tenant_id``
(see :func:`scoped_session`, wired into request handling in P2). ``SET LOCAL``
is transaction-scoped, so a pooled connection cannot carry one tenant's scope
into another tenant's request.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            settings.database_url,
            echo=settings.DB_ECHO,
            pool_size=settings.DB_POOL_SIZE,
            max_overflow=settings.DB_MAX_OVERFLOW,
            pool_pre_ping=True,
            pool_recycle=1800,
            # Server-side prepared statement caching interacts badly with
            # pgbouncer in transaction mode; disabling keeps deployment options
            # open at no meaningful cost at our query volume.
            connect_args={"statement_cache_size": 0},
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding an unscoped session.

    Unscoped sessions are for tenant-agnostic work only (health checks,
    authentication lookups by email). Anything touching tenant data must use
    the scoped dependency introduced in P2.
    """
    async with get_session_factory()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def scoped_session(
    tenant_id: UUID | str, *, actor: str = "user"
) -> AsyncIterator[AsyncSession]:
    """Session with the RLS tenant GUC bound for the life of the transaction.

    ``SET LOCAL`` (the ``true`` third argument to ``set_config``) reverts when
    the transaction ends, which is precisely the behaviour a pooled connection
    needs: one request's scope can never be inherited by the next.

    ``actor`` sets ``app.actor_kind``, which the verified-transaction trigger
    reads. A worker running AI categorisation passes ``"ai"``, and the database
    then refuses any write it attempts to a row a human has confirmed.
    """
    async with get_session_factory()() as session:
        async with session.begin():
            await session.execute(
                text(
                    "SELECT set_config('app.current_tenant_id', :tenant_id, true), "
                    "       set_config('app.actor_kind', :actor, true)"
                ),
                {"tenant_id": str(tenant_id), "actor": actor},
            )
            yield session


async def ping_database() -> bool:
    try:
        async with get_engine().connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
