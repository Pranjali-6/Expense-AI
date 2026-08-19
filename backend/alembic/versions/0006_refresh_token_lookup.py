"""Pre-authentication lookup path for refresh tokens

Revision ID: 0006_refresh_token_lookup
Revises: 0005_rule_thresholds_numeric
Create Date: 2026-08-18

Refreshing a session has the same shape as logging in: the caller presents a
credential, and the tenant is what that credential *resolves to*. It cannot be
known beforehand, so the lookup has to happen without a tenant scope — and
under FORCE ROW LEVEL SECURITY it returned nothing at all, making every refresh
fail with "session expired".

The fix mirrors what ``users`` and ``tenants`` already do. Those three tables
are the entire pre-authentication surface:

    tenants         the tenant being resolved
    users           resolve an email to an account
    refresh_tokens  resolve a token hash to an account

Each keeps RLS **enabled**, so the application role is fully policed for every
ordinary query. Each drops FORCE, so a SECURITY DEFINER function owned by the
table owner can perform exactly one narrow cross-tenant lookup. The function
returns only the columns a session decision needs, and takes a 256-bit hash as
its argument — there is nothing to enumerate.
"""

from __future__ import annotations

from typing import Sequence

from alembic import op

revision: str = "0006_refresh_token_lookup"
down_revision: str | None = "0005_rule_thresholds_numeric"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE refresh_tokens NO FORCE ROW LEVEL SECURITY;")

    op.execute(
        """
        CREATE OR REPLACE FUNCTION auth_lookup_refresh_token(p_token_hash varchar)
        RETURNS TABLE (
            id          uuid,
            tenant_id   uuid,
            user_id     uuid,
            family_id   uuid,
            expires_at  timestamptz,
            revoked_at  timestamptz,
            rotated_to  uuid
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = public
        AS $$
            SELECT r.id, r.tenant_id, r.user_id, r.family_id,
                   r.expires_at, r.revoked_at, r.rotated_to
            FROM refresh_tokens r
            WHERE r.token_hash = p_token_hash
        $$;
        """
    )

    op.execute(
        """
        DO $$
        DECLARE app_role text := 'expense_app';
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = app_role) THEN
                EXECUTE format(
                    'GRANT EXECUTE ON FUNCTION auth_lookup_refresh_token(varchar) TO %I',
                    app_role);
            END IF;
        END;
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS auth_lookup_refresh_token(varchar);")
    op.execute("ALTER TABLE refresh_tokens FORCE ROW LEVEL SECURITY;")
