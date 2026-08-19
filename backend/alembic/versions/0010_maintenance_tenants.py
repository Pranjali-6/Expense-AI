"""Let scheduled maintenance enumerate tenants, narrowly.

``tenants`` has Row Level Security enabled but not FORCED, which protects the
owner and not the application role — so an unscoped session sees **zero**
tenants, not all of them. That is the correct behaviour and it is also a trap:
a maintenance job written against it does not fail, it quietly iterates an
empty list and concludes there is nothing to do.

It cost me exactly that. A reconciliation job that deleted stored objects "not
referenced by any tenant" enumerated no tenants, built an empty reference set,
and deleted the entire bucket. The bug was one wrong assumption about a policy;
the damage came from a job that treated an empty list as a valid answer.

So: one more ``SECURITY DEFINER`` function, in the same style as
``auth_lookup_user_by_email`` and ``ops_platform_counters``. It returns tenant
ids and nothing else — no name, no email, no row from any scoped table — and it
takes no arguments, so it cannot be aimed. A caller that gets an empty result
from *this* now knows there genuinely are no tenants.

Revision ID: 0010_maintenance_tenants
Revises: 0009_platform_counters
"""

from __future__ import annotations

from alembic import op

revision = "0010_maintenance_tenants"
down_revision = "0009_platform_counters"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION ops_active_tenants()
        RETURNS TABLE (tenant_id uuid)
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = public
        AS $$
            SELECT t.id FROM tenants t WHERE t.deleted_at IS NULL
        $$;
        """
    )
    op.execute("REVOKE ALL ON FUNCTION ops_active_tenants() FROM PUBLIC")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'expense_app') THEN
                GRANT EXECUTE ON FUNCTION ops_active_tenants() TO expense_app;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS ops_active_tenants()")
