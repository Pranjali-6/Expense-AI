"""Aggregate counters for the metrics endpoint, without a bypass.

The operational dashboard needs three numbers that are properties of the whole
deployment rather than of any tenant: how deep the review queue is, how large
the ledger is, how many statements failed reconciliation. Row Level Security
correctly refuses to answer — the application connects as a non-superuser with
``FORCE ROW LEVEL SECURITY`` and no tenant scope, so those queries return zero,
which is the fail-closed behaviour working exactly as designed.

The wrong fix is to connect as the owner, or to exempt the tables. Either
converts a targeted need for three integers into a general ability to read
everyone's transactions, and it would sit in the codebase forever as a
precedent.

The right fix is the same one authentication already uses: one narrow
``SECURITY DEFINER`` function that crosses the boundary in a single, auditable
place and returns **only aggregates**. It takes no arguments, so it cannot be
pointed at a tenant. It returns three ``bigint`` counts, so there is no row, no
identifier, no amount and no merchant to leak. Reading it tells you the size of
the system and nothing about anyone in it — which is the same rule the logging
policy applies to log lines.

Revision ID: 0009_platform_counters
Revises: 0008_assistant_incidents
"""

from __future__ import annotations

from alembic import op

revision = "0009_platform_counters"
down_revision = "0008_assistant_incidents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION ops_platform_counters()
        RETURNS TABLE (
            review_queue_depth   bigint,
            ledger_transactions  bigint,
            untrusted_statements bigint
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = public
        AS $$
            SELECT
                (SELECT count(*) FROM transactions
                  WHERE review_status = 'review_required'),
                (SELECT count(*) FROM transactions),
                (SELECT count(*) FROM statements
                  WHERE trust_status = 'untrusted' AND deleted_at IS NULL)
        $$;
        """
    )
    # No argument means nothing to inject; no rows means nothing to leak. The
    # grant is to the application role only — PUBLIC is deliberately revoked,
    # because a function that bypasses RLS should be callable by exactly the
    # roles that need it.
    op.execute("REVOKE ALL ON FUNCTION ops_platform_counters() FROM PUBLIC")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'expense_app') THEN
                GRANT EXECUTE ON FUNCTION ops_platform_counters() TO expense_app;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS ops_platform_counters()")
