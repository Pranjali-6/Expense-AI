"""Content-hash uniqueness must ignore soft-deleted statements.

``UNIQUE (tenant_id, file_sha256)`` was unconditional, so a statement that had
been deleted still reserved its content hash forever. Deleting a statement and
re-uploading the same PDF — the obvious way to recover from a bad import —
raised a unique violation and surfaced as a 500.

The application already had the right rule: its duplicate check reads
``WHERE file_sha256 = :digest AND deleted_at IS NULL``. The constraint simply
did not agree with it. A partial unique index makes the database enforce the
same rule the code intends, which is the only version of the rule that counts.

Revision ID: 0007_statement_hash_partial
Revises: 0006_refresh_token_lookup
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_statement_hash_partial"
down_revision = "0006_refresh_token_lookup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "uq_statements_tenant_id_file_sha256", "statements", type_="unique"
    )
    op.create_index(
        "uq_statements_tenant_id_file_sha256",
        "statements",
        ["tenant_id", "file_sha256"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_statements_tenant_id_file_sha256", table_name="statements")
    op.create_unique_constraint(
        "uq_statements_tenant_id_file_sha256", "statements", ["tenant_id", "file_sha256"]
    )
