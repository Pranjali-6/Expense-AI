"""Look up a tenant by slug without guessing that empty means absent.

The demo seed checked whether its tenant already existed with a plain
``SELECT id FROM tenants WHERE slug = 'demo'``. Row Level Security is enabled
on ``tenants``, and the seed connects as the application role, so that query
returns zero rows whether or not the tenant exists. The check therefore never
worked — it only ever *agreed*, because on an empty database "no rows" and "not
there" are the same answer.

It stopped agreeing the first time someone re-ran ``make bootstrap`` against a
database that already had the demo tenant: the lookup said absent, the insert
said `duplicate key value violates unique constraint "uq_tenants_slug"`, and a
seed documented as idempotent was not.

This is the same shape as the bug that emptied the object bucket during P9 —
an RLS-scoped read returning nothing, and code treating nothing as proof of
absence. The fix is the same too, and it is the one this codebase already uses
for pre-authentication lookups (``auth_lookup_user_by_email``): one narrow
``SECURITY DEFINER`` function that crosses the boundary in a single auditable
place and returns a single id. A slug is not a secret, the function returns no
row data, and an empty result from *this* genuinely means absent.

Revision ID: 0011_tenant_slug_lookup
Revises: 0010_maintenance_tenants
"""

from __future__ import annotations

from alembic import op

revision = "0011_tenant_slug_lookup"
down_revision = "0010_maintenance_tenants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION ops_tenant_id_by_slug(p_slug varchar)
        RETURNS uuid
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = public
        AS $$
            SELECT t.id FROM tenants t
            WHERE t.slug = p_slug AND t.deleted_at IS NULL
        $$;
        """
    )
    op.execute("REVOKE ALL ON FUNCTION ops_tenant_id_by_slug(varchar) FROM PUBLIC")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'expense_app') THEN
                GRANT EXECUTE ON FUNCTION ops_tenant_id_by_slug(varchar) TO expense_app;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS ops_tenant_id_by_slug(varchar)")
