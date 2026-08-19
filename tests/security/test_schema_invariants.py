"""Schema-level invariants.

These assert properties of the schema itself rather than of any one query, so a
model or migration that quietly breaks one fails here instead of in production
six weeks later.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Base


class TestMoneyPrecision:
    def test_no_monetary_column_is_a_floating_point_type(self) -> None:
        """The invariant behind every number this product reports.

        A single FLOAT column is enough to make a reconciliation that should be
        exactly zero come out at 0.000000001 — and then a statement that
        balances perfectly is reported as untrusted.
        """
        offenders = [
            f"{table.name}.{column.name} ({column.type})"
            for table in Base.metadata.sorted_tables
            for column in table.columns
            if type(column.type).__name__ in {"Float", "REAL", "DOUBLE_PRECISION"}
        ]
        assert not offenders, f"floating-point columns found: {offenders}"

    def test_transaction_amounts_are_numeric_18_2(self) -> None:
        transactions = Base.metadata.tables["transactions"]
        for name in ("original_amount", "corrected_amount", "amount", "original_balance_after"):
            column = transactions.columns[name]
            assert column.type.precision == 18, f"{name} precision"
            assert column.type.scale == 2, f"{name} scale"


class TestDefaults:
    def test_every_defaulted_not_null_column_has_a_database_default(self) -> None:
        """Python-side defaults do not survive raw SQL, COPY, or psql.

        Found the hard way: a raw INSERT omitting ``users.failed_login_count``
        hit a NOT NULL violation because the default existed only in the ORM.
        """
        offenders = [
            f"{table.name}.{column.name}"
            for table in Base.metadata.sorted_tables
            for column in table.columns
            if column.default is not None
            and not column.default.is_sequence
            and not column.default.is_callable
            and column.server_default is None
            and not column.nullable
            and column.computed is None
        ]
        assert not offenders, f"NOT NULL columns without a database default: {offenders}"


class TestGeneratedColumnDefinitions:
    async def test_effective_columns_are_generated_not_writable(
        self, session: AsyncSession
    ) -> None:
        """The dual-value design only holds if the effective value is computed.

        If ``amount`` were an ordinary column, some code path would eventually
        set it inconsistently with ``original_amount`` and ``corrected_amount``,
        and nobody would notice.
        """
        rows = (
            await session.execute(
                text(
                    """
                    SELECT column_name, is_generated
                    FROM information_schema.columns
                    WHERE table_name = 'transactions'
                      AND column_name IN (
                        'txn_date', 'description', 'amount', 'direction',
                        'merchant', 'payment_method', 'confidence_min'
                      )
                    """
                )
            )
        ).all()

        assert len(rows) == 7
        for name, is_generated in rows:
            assert is_generated == "ALWAYS", f"{name} is not a generated column"

    async def test_confidence_min_is_defined_as_least_not_as_an_average(
        self, session: AsyncSession
    ) -> None:
        expression = await session.scalar(
            text(
                """
                SELECT generation_expression
                FROM information_schema.columns
                WHERE table_name = 'transactions' AND column_name = 'confidence_min'
                """
            )
        )
        assert expression is not None
        assert "LEAST" in expression.upper()
        assert "/" not in expression, "confidence_min appears to be averaging"


class TestConstraints:
    async def test_the_duplicate_guard_excludes_running_balance(
        self, session: AsyncSession
    ) -> None:
        """Deliberate, and the reason deduplication works across re-issues.

        The same transaction carries different balances on a corrected or
        overlapping statement. A fingerprint that included balance would hash
        differently for an identical transaction and let the duplicate through.
        """
        definition = await session.scalar(
            text(
                """
                SELECT pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE conname = 'uq_transactions_tenant_id_account_id_fingerprint'
                """
            )
        )
        assert definition is not None
        assert "tenant_id" in definition
        assert "account_id" in definition
        assert "fingerprint" in definition
        assert "balance" not in definition

    async def test_a_verified_transaction_must_name_its_verifier(
        self, session: AsyncSession
    ) -> None:
        definition = await session.scalar(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_transactions_verified_has_actor'"
            )
        )
        assert definition is not None
        assert "verified_by" in definition
        assert "verified_at" in definition


class TestMigrationParity:
    async def test_the_database_matches_the_models(self, session: AsyncSession) -> None:
        """No drift between the models and the applied migrations.

        Catches the common failure where a column is added to a model and
        everything works locally because the developer's database was recreated,
        while production never got the migration.
        """
        db_tables = set(
            (
                await session.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
                    )
                )
            ).scalars().all()
        )
        db_tables.discard("alembic_version")

        model_tables = set(Base.metadata.tables)
        assert model_tables == db_tables, (
            f"only in models: {sorted(model_tables - db_tables)}; "
            f"only in database: {sorted(db_tables - model_tables)}"
        )

    async def test_every_model_column_exists_in_the_database(
        self, session: AsyncSession
    ) -> None:
        rows = (
            await session.execute(
                text(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public'"
                )
            )
        ).all()
        db_columns = {(table, column) for table, column in rows}

        missing = [
            (table.name, column.name)
            for table in Base.metadata.sorted_tables
            for column in table.columns
            if (table.name, column.name) not in db_columns
        ]
        assert not missing, f"columns in models but not in the database: {missing}"
