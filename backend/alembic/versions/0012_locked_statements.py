"""Hold password-protected statements instead of discarding them.

A password-protected PDF used to be rejected at the door: nothing was stored,
so there was nothing to retry against and the only recovery was to upload the
file again. Worse, supplying the *correct* password passed validation and then
stored the still-encrypted bytes, because the password was used for the
structural check and then dropped on the floor. Extraction opened those bytes
with no password, read zero pages, and the statement failed with no useful
reason. Knowing your password produced a worse outcome than not knowing it.

Now the file is stored and parked in `password_required`, which is a waiting
state rather than a failure. `POST /statements/{id}/unlock` supplies the
password per file — batch upload could only ever apply one password to twelve
statements from different banks — and the pipeline resumes.

`unlock_attempts` caps how many guesses a statement will accept. It lives on
the row rather than in Redis because it is a security control and a cache flush
must not grant a fresh budget. Indian banks document their statement password
formulas, and they are built from PAN, date of birth and account digits, so an
uncapped unlock endpoint is a practical oracle against precisely the
identifiers the rest of this system works to keep out of reach.

Revision ID: 0012_locked_statements
Revises: 0011_tenant_slug_lookup
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_locked_statements"
down_revision = "0011_tenant_slug_lookup"
branch_labels = None
depends_on = None

_STATUS_CHECK = "ck_statements_status_valid"
_OLD = "status IN ('uploaded', 'processing', 'processed', 'failed')"
_NEW = (
    "status IN ('uploaded', 'processing', 'processed', 'failed', "
    "'password_required')"
)


def upgrade() -> None:
    op.add_column(
        "statements",
        sa.Column(
            "unlock_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )

    # `op.f()` marks a name as already rendered. The naming convention prefixes
    # whatever it is handed, so a bare `ck_statements_status_valid` here becomes
    # `ck_statements_ck_statements_status_valid` and the drop fails. Create takes
    # the short name and lets the convention build it; drop takes the built one.
    op.drop_constraint(op.f(_STATUS_CHECK), "statements", type_="check")
    op.create_check_constraint("status_valid", "statements", _NEW)

    # Partial, and on tenant_id first, because the only query is "which of my
    # statements are waiting for a password?" — rendered as a badge on the
    # statements list, so it runs on every page load.
    op.create_index(
        "ix_statements_awaiting_password",
        "statements",
        ["tenant_id"],
        postgresql_where=sa.text("status = 'password_required'"),
    )


def downgrade() -> None:
    op.drop_index("ix_statements_awaiting_password", table_name="statements")

    # Nothing sensible to migrate these to. A locked statement has no
    # transactions and its stored object is unreadable without a password the
    # database never held, so `failed` is the honest terminal state.
    op.execute(
        "UPDATE statements SET status = 'failed', "
        "error_code = 'password_required' "
        "WHERE status = 'password_required'"
    )

    op.drop_constraint(op.f(_STATUS_CHECK), "statements", type_="check")
    op.create_check_constraint("status_valid", "statements", _OLD)
    op.drop_column("statements", "unlock_attempts")
