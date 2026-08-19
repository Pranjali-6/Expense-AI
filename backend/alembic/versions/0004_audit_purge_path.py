"""Separate audit immutability from audit erasure

Revision ID: 0004_audit_purge_path
Revises: 0003_server_defaults
Create Date: 2026-08-18

The first cut of the append-only trigger blocked UPDATE and DELETE alike. That
is right for tampering and wrong for erasure: when a user deletes their account,
their audit rows have to go too, and a blanket DELETE block also breaks
``ON DELETE CASCADE`` from the parent transaction — the audit trail would pin
data the user asked to have removed.

So the two concerns are split:

*   **UPDATE is refused unconditionally.** An audit entry that can be edited is
    not an audit entry, and there is no legitimate reason to rewrite one.
*   **DELETE is refused unless ``app.allow_audit_purge`` is set**, which only
    the data-deletion path does. Erasure stays possible, stays deliberate, and
    stays greppable — a DELETE without that flag still fails.
"""

from __future__ import annotations

from typing import Sequence

from alembic import op

revision: str = "0004_audit_purge_path"
down_revision: str | None = "0003_server_defaults"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AUDIT_TABLES = ("audit_logs", "transaction_audit")


def upgrade() -> None:
    for table in AUDIT_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table};")
    op.execute("DROP FUNCTION IF EXISTS reject_mutation();")

    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_audit_update() RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only and cannot be modified', TG_TABLE_NAME
                USING ERRCODE = 'insufficient_privilege';
        END;
        $$;
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_audit_delete() RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            -- Erasure is permitted, but only when the caller has explicitly
            -- opted in for this transaction. An accidental DELETE still fails.
            IF COALESCE(NULLIF(current_setting('app.allow_audit_purge', true), ''), 'off')
               <> 'on' THEN
                RAISE EXCEPTION
                    '% is append-only; set app.allow_audit_purge to erase',
                    TG_TABLE_NAME
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            RETURN OLD;
        END;
        $$;
        """
    )

    for table in AUDIT_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_no_update
                BEFORE UPDATE ON {table}
                FOR EACH ROW EXECUTE FUNCTION reject_audit_update();
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_guarded_delete
                BEFORE DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION reject_audit_delete();
            """
        )


def downgrade() -> None:
    for table in AUDIT_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_update ON {table};")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_guarded_delete ON {table};")
    op.execute("DROP FUNCTION IF EXISTS reject_audit_delete();")
    op.execute("DROP FUNCTION IF EXISTS reject_audit_update();")

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
    for table in AUDIT_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_append_only
                BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION reject_mutation();
            """
        )
