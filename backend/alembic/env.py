"""Alembic environment.

Migrations connect as ``POSTGRES_USER`` (the owner), not as the application
role. The application role is created ``NOSUPERUSER NOBYPASSRLS`` precisely so
Row Level Security applies to it, which also means it cannot issue DDL — the
two roles have different jobs and neither should be able to do the other's.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# The backend package lives one level up from this file.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.models import Base  # noqa: E402  (imports every model onto the metadata)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", settings.migration_database_url)

target_metadata = Base.metadata


def include_object(obj, name: str, type_: str, reflected: bool, compare_to) -> bool:
    """Keep autogenerate away from objects Alembic cannot model.

    Row Level Security policies, triggers and functions are created by explicit
    migrations. Autogenerate does not understand them, and left unfiltered it
    would happily propose dropping the very policies enforcing tenant isolation.
    """
    if type_ == "table" and name in {"spatial_ref_sys"}:
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=settings.migration_database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            compare_type=True,
            compare_server_default=True,
            # One transaction per migration: a failed migration leaves nothing
            # half-applied.
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
