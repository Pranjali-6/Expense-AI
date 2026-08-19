"""Record an assistant answer that failed the traceability check.

The privacy-incident kinds were written for the categorisation path, where
every failure is about something that should not have gone *out*. The assistant
adds a failure about something that should not have come *back*: a figure in a
generated answer that appears in no tool result — invented, derived or
misremembered, and indistinguishable from the outside.

It belongs in the same table rather than a new one. The Privacy Center's
promise is a single honest account of what the model did, and splitting "we
blocked a payload" from "we discarded an answer" across two places would let a
clean incident log coexist with a model that was quietly making numbers up.

The answer itself is never stored. The row records that it happened and which
kind of figure was untraceable — a currency amount, a percentage, a count —
because the figures are the user's financial data and an incident log is not
where that belongs.

Revision ID: 0008_assistant_incidents
Revises: 0007_statement_hash_partial
"""

from __future__ import annotations

from alembic import op

revision = "0008_assistant_incidents"
down_revision = "0007_statement_hash_partial"
branch_labels = None
depends_on = None

_OLD = (
    "kind IN ('pii_in_payload', 'injection_quarantined', 'output_pii_echo', "
    "'output_schema_violation', 'budget_exceeded')"
)
_NEW = (
    "kind IN ('pii_in_payload', 'injection_quarantined', 'output_pii_echo', "
    "'output_schema_violation', 'budget_exceeded', 'output_untraceable_figure')"
)


# The metadata naming convention is ``ck_%(table_name)s_%(constraint_name)s``,
# so the short name goes to ``create_check_constraint`` and the rendered one is
# wrapped in ``op.f`` for the drop. Passing the rendered name to both prefixes
# it twice and the migration fails looking for a constraint nobody named.
_SHORT = "kind_valid"
_RENDERED = "ck_privacy_incidents_kind_valid"


def upgrade() -> None:
    op.drop_constraint(op.f(_RENDERED), "privacy_incidents", type_="check")
    op.create_check_constraint(_SHORT, "privacy_incidents", _NEW)


def downgrade() -> None:
    # Rows carrying the new kind would violate the narrower constraint, so they
    # go first. Losing incident history is bad; a migration that cannot run is
    # worse, and a downgrade is by definition discarding what the newer schema
    # knew how to record.
    op.execute(
        "DELETE FROM privacy_incidents WHERE kind = 'output_untraceable_figure'"
    )
    op.drop_constraint(op.f(_RENDERED), "privacy_incidents", type_="check")
    op.create_check_constraint(_SHORT, "privacy_incidents", _OLD)
