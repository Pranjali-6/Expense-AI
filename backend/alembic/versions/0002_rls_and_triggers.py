"""Row Level Security, integrity triggers and role grants

Revision ID: 0002_rls_and_triggers
Revises: 0001_initial_schema
Create Date: 2026-08-18

This migration is the security boundary. Everything in it is written out
explicitly rather than discovered at runtime, because reading this file should
tell you exactly which tables are protected and how — a loop over
``information_schema`` would be shorter and considerably harder to audit.

``tests/security/test_rls.py`` asserts that every table carrying a ``tenant_id``
has a policy, so a table added later without one fails the suite instead of
quietly becoming readable across tenants.
"""

from __future__ import annotations

from typing import Sequence

from alembic import op

revision: str = "0002_rls_and_triggers"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Every table with a tenant_id column. Ordered as in the model registry.
TENANT_SCOPED_TABLES: tuple[str, ...] = (
    "accounts",
    "ai_classifications",
    "anomalies",
    "audit_logs",
    "budgets",
    "ingestion_sources",
    "insight_snapshots",
    "job_events",
    "notifications",
    "privacy_counters",
    "privacy_incidents",
    "processing_jobs",
    "refresh_tokens",
    "statement_health",
    "statement_pages",
    "statements",
    "subscriptions",
    "timeline_events",
    "transaction_audit",
    "transactions",
    "transfer_groups",
    "user_category_rules",
    "users",
)

# FORCE ROW LEVEL SECURITY makes policies apply to the table owner too, which
# closes the hole where a compromised owner connection reads everything.
#
# Two tables are deliberately excluded:
#
#   users    — authentication happens *before* any tenant is known. Resolving an
#              email to an account has to cross the boundary exactly once, and
#              it does so through the narrow SECURITY DEFINER function below
#              rather than through a blanket exemption.
#   tenants  — same reason: the tenant is the thing being looked up.
#
# Both still have RLS enabled, so the application role remains fully policed.
# Only the owner — which the application never connects as — can bypass them.
NOT_FORCED: frozenset[str] = frozenset({"users", "tenants"})


def upgrade() -> None:
    # ---------------------------------------------------------------- helper --
    # NULLIF + a NULL result is what makes this fail closed: when the GUC is
    # unset, `tenant_id = NULL` evaluates to NULL, the policy does not pass, and
    # the query returns nothing. A forgotten SET LOCAL yields zero rows, never
    # everyone's rows.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_current_tenant() RETURNS uuid
        LANGUAGE sql STABLE
        AS $$
            SELECT NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
        $$;
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_current_actor_kind() RETURNS text
        LANGUAGE sql STABLE
        AS $$
            SELECT COALESCE(NULLIF(current_setting('app.actor_kind', true), ''), 'system')
        $$;
        """
    )

    # ------------------------------------------------------------ updated_at --
    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$;
        """
    )

    # Applied to every table that has the column, so raw SQL and background
    # jobs cannot leave a stale timestamp the way an ORM-only default would.
    op.execute(
        """
        DO $$
        DECLARE tbl text;
        BEGIN
            FOR tbl IN
                SELECT c.relname
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_attribute a ON a.attrelid = c.oid
                WHERE n.nspname = 'public'
                  AND c.relkind = 'r'
                  AND a.attname = 'updated_at'
                  AND NOT a.attisdropped
            LOOP
                EXECUTE format(
                    'CREATE TRIGGER trg_%1$s_set_updated_at
                       BEFORE UPDATE ON %1$I
                       FOR EACH ROW EXECUTE FUNCTION set_updated_at()',
                    tbl
                );
            END LOOP;
        END;
        $$;
        """
    )

    # ------------------------------------------- immutability of originals --
    # The plan's guarantee is that extracted values are preserved forever. A
    # service-layer rule would hold right up until the first bulk UPDATE written
    # in a hurry, so the database enforces it.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION freeze_transaction_originals() RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.original_txn_date        IS DISTINCT FROM OLD.original_txn_date
            OR NEW.original_value_date      IS DISTINCT FROM OLD.original_value_date
            OR NEW.original_description     IS DISTINCT FROM OLD.original_description
            OR NEW.original_amount          IS DISTINCT FROM OLD.original_amount
            OR NEW.original_direction       IS DISTINCT FROM OLD.original_direction
            OR NEW.original_balance_after   IS DISTINCT FROM OLD.original_balance_after
            OR NEW.original_reference       IS DISTINCT FROM OLD.original_reference
            OR NEW.original_merchant        IS DISTINCT FROM OLD.original_merchant
            OR NEW.original_payment_method  IS DISTINCT FROM OLD.original_payment_method
            OR NEW.original_category_id     IS DISTINCT FROM OLD.original_category_id
            OR NEW.original_subcategory_id  IS DISTINCT FROM OLD.original_subcategory_id
            THEN
                -- The message carries an id and nothing else: it will end up in
                -- a log, and a log may not contain financial values.
                RAISE EXCEPTION
                    'original_* columns are immutable (transaction %)', OLD.id
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )

    op.execute(
        """
        CREATE TRIGGER trg_transactions_freeze_originals
            BEFORE UPDATE ON transactions
            FOR EACH ROW EXECUTE FUNCTION freeze_transaction_originals();
        """
    )

    # ------------------------------------------- protection of verified rows --
    # The AI never overrides a human. The categorisation worker sets
    # `app.actor_kind = 'ai'` for the duration of its transaction, and this
    # trigger refuses any write it makes to a row a person has confirmed.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION protect_verified_transactions() RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF OLD.is_verified AND app_current_actor_kind() = 'ai' THEN
                RAISE EXCEPTION
                    'AI may not modify a verified transaction (%)', OLD.id
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )

    op.execute(
        """
        CREATE TRIGGER trg_transactions_protect_verified
            BEFORE UPDATE ON transactions
            FOR EACH ROW EXECUTE FUNCTION protect_verified_transactions();
        """
    )

    # ----------------------------------------------------- append-only audit --
    # An audit trail that can be edited is not an audit trail.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_mutation() RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only', TG_TABLE_NAME
                USING ERRCODE = 'insufficient_privilege';
        END;
        $$;
        """
    )

    for table in ("audit_logs", "transaction_audit"):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_append_only
                BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION reject_mutation();
            """
        )

    # -------------------------------------------------------- tenant policies --
    for table in TENANT_SCOPED_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        if table not in NOT_FORCED:
            op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
                USING (tenant_id = app_current_tenant())
                WITH CHECK (tenant_id = app_current_tenant());
            """
        )

    # `tenants` is isolated on its primary key rather than a tenant_id column.
    op.execute("ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON tenants
            USING (id = app_current_tenant())
            WITH CHECK (id = app_current_tenant());
        """
    )

    # ------------------------------------------------- pre-auth lookup path --
    # The one operation that legitimately has no tenant context yet. Rather than
    # exempting `users` from RLS for the application, expose a single function
    # that returns only the columns a login decision needs — no name, no email
    # beyond what was supplied, nothing about the user's finances.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION auth_lookup_user(p_email citext)
        RETURNS TABLE (
            id                 uuid,
            tenant_id          uuid,
            password_hash      varchar,
            auth_provider      varchar,
            status             varchar,
            role               varchar,
            failed_login_count integer,
            locked_until       timestamptz
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = public
        AS $$
            SELECT u.id, u.tenant_id, u.password_hash, u.auth_provider,
                   u.status, u.role, u.failed_login_count, u.locked_until
            FROM users u
            WHERE u.email = p_email
              AND u.deleted_at IS NULL
        $$;
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION auth_lookup_user_by_google(p_subject varchar)
        RETURNS TABLE (
            id                 uuid,
            tenant_id          uuid,
            auth_provider      varchar,
            status             varchar,
            role               varchar
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = public
        AS $$
            SELECT u.id, u.tenant_id, u.auth_provider, u.status, u.role
            FROM users u
            WHERE u.google_subject = p_subject
              AND u.deleted_at IS NULL
        $$;
        """
    )

    # ------------------------------------------------------------- grants ----
    # Default privileges already cover tables created by the owner, but stating
    # them here means a fresh database is correct even if the init script that
    # set those defaults was ever changed.
    op.execute(
        """
        DO $$
        DECLARE app_role text := current_setting('app.bootstrap_role', true);
        BEGIN
            IF app_role IS NULL OR app_role = '' THEN
                app_role := 'expense_app';
            END IF;

            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = app_role) THEN
                EXECUTE format(
                    'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES '
                    'IN SCHEMA public TO %I', app_role);
                EXECUTE format(
                    'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO %I',
                    app_role);
                EXECUTE format(
                    'GRANT EXECUTE ON FUNCTION auth_lookup_user(citext) TO %I',
                    app_role);
                EXECUTE format(
                    'GRANT EXECUTE ON FUNCTION auth_lookup_user_by_google(varchar) TO %I',
                    app_role);
                EXECUTE format(
                    'GRANT EXECUTE ON FUNCTION app_current_tenant() TO %I', app_role);
                EXECUTE format(
                    'GRANT EXECUTE ON FUNCTION app_current_actor_kind() TO %I', app_role);
            END IF;
        END;
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS auth_lookup_user_by_google(varchar);")
    op.execute("DROP FUNCTION IF EXISTS auth_lookup_user(citext);")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON tenants;")
    op.execute("ALTER TABLE tenants DISABLE ROW LEVEL SECURITY;")

    for table in TENANT_SCOPED_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table};")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")

    for table in ("audit_logs", "transaction_audit"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table};")
    op.execute("DROP FUNCTION IF EXISTS reject_mutation();")

    op.execute(
        "DROP TRIGGER IF EXISTS trg_transactions_protect_verified ON transactions;"
    )
    op.execute("DROP FUNCTION IF EXISTS protect_verified_transactions();")

    op.execute(
        "DROP TRIGGER IF EXISTS trg_transactions_freeze_originals ON transactions;"
    )
    op.execute("DROP FUNCTION IF EXISTS freeze_transaction_originals();")

    op.execute(
        """
        DO $$
        DECLARE tbl text;
        BEGIN
            FOR tbl IN
                SELECT c.relname
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_attribute a ON a.attrelid = c.oid
                WHERE n.nspname = 'public'
                  AND c.relkind = 'r'
                  AND a.attname = 'updated_at'
                  AND NOT a.attisdropped
            LOOP
                EXECUTE format(
                    'DROP TRIGGER IF EXISTS trg_%1$s_set_updated_at ON %1$I', tbl);
            END LOOP;
        END;
        $$;
        """
    )
    op.execute("DROP FUNCTION IF EXISTS set_updated_at();")
    op.execute("DROP FUNCTION IF EXISTS app_current_actor_kind();")
    op.execute("DROP FUNCTION IF EXISTS app_current_tenant();")
