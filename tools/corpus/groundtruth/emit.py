"""Write reviewed rows out as ``<fixture>.expected.json``.

The shape is the synthetic generator's, field for field, because the accuracy
harness reads both through one loader. If these two drift, `make accuracy` and
`make validate-real` stop measuring the same thing and the real scorecard
becomes incomparable to the synthetic one.

Money is written as a **string** for the same reason the generator does it:
JSON numbers are doubles, and ground truth that cannot round-trip ₹1,23,456.78
exactly is not ground truth.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

#: Bumped when the *builder* changes shape, mirroring GENERATOR_VERSION on the
#: synthetic side. Distinct from it so a fixture's provenance is never
#: ambiguous: a real fixture is reviewed, not generated.
BUILDER_VERSION = 1

REQUIRED_ROW_FIELDS = ("txn_date", "description", "amount", "direction")

OPTIONAL_ROW_FIELDS = (
    "value_date", "balance_after", "reference", "merchant_normalized",
    "merchant_slug", "payment_method", "category_slug", "subcategory_slug",
)


class ReviewIncomplete(ValueError):
    """The review is not finished, or the rows do not hold together."""


def _decimal(value: Any, *, field: str, index: int) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ReviewIncomplete(f"row {index + 1}: {field} is not a number: {value!r}") from None


def _date(value: Any, *, field: str, index: int) -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise ReviewIncomplete(
            f"row {index + 1}: {field} must be YYYY-MM-DD, got {value!r}"
        ) from None


def validate(rows: list[dict[str, Any]], metadata: dict[str, Any]) -> list[str]:
    """Check the review before it is written. Returns advisory warnings.

    Anything that would make the fixture *wrong* raises; anything that merely
    makes it weaker is returned for the reviewer to see and decide about.
    """
    if not rows:
        raise ReviewIncomplete("no transactions — a fixture with no rows proves nothing")

    unconfirmed = [i + 1 for i, row in enumerate(rows) if not row.get("confirmed")]
    if unconfirmed:
        raise ReviewIncomplete(
            f"{len(unconfirmed)} row(s) not confirmed: "
            f"{', '.join(str(n) for n in unconfirmed[:10])}"
            f"{' …' if len(unconfirmed) > 10 else ''}"
        )

    for index, row in enumerate(rows):
        for field in REQUIRED_ROW_FIELDS:
            if row.get(field) in (None, ""):
                raise ReviewIncomplete(f"row {index + 1}: {field} is required")
        _date(row["txn_date"], field="txn_date", index=index)
        _decimal(row["amount"], field="amount", index=index)
        if row["direction"] not in ("debit", "credit"):
            raise ReviewIncomplete(
                f"row {index + 1}: direction must be debit or credit, "
                f"got {row['direction']!r}"
            )

    warnings: list[str] = []

    if metadata.get("opening_balance") in (None, ""):
        warnings.append(
            "no opening balance: the harness cannot check this statement reconciles"
        )
    else:
        opening = Decimal(str(metadata["opening_balance"]))
        running = opening
        drifted: list[int] = []
        for index, row in enumerate(rows):
            amount = Decimal(str(row["amount"]))
            running += -amount if row["direction"] == "debit" else amount
            stated = row.get("balance_after")
            if stated in (None, ""):
                continue
            if Decimal(str(stated)) != running:
                drifted.append(index + 1)
                running = Decimal(str(stated))
        if drifted:
            # Not fatal: some statements genuinely omit or reorder balances.
            # But a drifting balance is the single loudest signal that a row is
            # wrong, so it is never swallowed.
            warnings.append(
                f"running balance does not follow at row(s) "
                f"{', '.join(str(n) for n in drifted[:10])}"
                f"{' …' if len(drifted) > 10 else ''}"
            )

    dates = [_date(row["txn_date"], field="txn_date", index=i) for i, row in enumerate(rows)]
    if dates != sorted(dates):
        warnings.append("transactions are not in date order")

    return warnings


def build(
    rows: list[dict[str, Any]],
    metadata: dict[str, Any],
    *,
    fixture: str,
    page_count: int,
    notes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the expected.json payload. Assumes `validate` has passed."""
    debits = sum(
        (Decimal(str(row["amount"])) for row in rows if row["direction"] == "debit"),
        Decimal("0.00"),
    )
    credits = sum(
        (Decimal(str(row["amount"])) for row in rows if row["direction"] == "credit"),
        Decimal("0.00"),
    )

    opening = (
        Decimal(str(metadata["opening_balance"]))
        if metadata.get("opening_balance") not in (None, "")
        else None
    )
    is_card = metadata.get("document_type") == "credit_card_statement"
    expected_closing = (
        None if opening is None
        else (opening + debits - credits) if is_card
        else (opening - debits + credits)
    )

    payload_notes = {
        "corpus": "real",
        "source": "human-reviewed parser proposal",
        "builder": "tools.corpus.groundtruth",
        "reviewed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    payload_notes.update(notes or {})

    def optional(row: dict[str, Any], field: str) -> Any:
        value = row.get(field)
        return value if value not in ("",) else None

    return {
        "fixture": fixture,
        "generator_version": BUILDER_VERSION,
        # No seed: this statement was issued by a bank, not generated. Kept as
        # an explicit null so the key set still matches the synthetic side.
        "seed": None,
        "page_count": page_count,
        "notes": payload_notes,
        "metadata": {
            "bank_code": metadata.get("bank_code"),
            "bank_name": metadata.get("bank_name"),
            "document_type": metadata.get("document_type"),
            "account_last4": metadata.get("account_last4") or None,
            "period_start": metadata.get("period_start") or None,
            "period_end": metadata.get("period_end") or None,
            "opening_balance": str(opening) if opening is not None else None,
            "closing_balance": (
                str(Decimal(str(metadata["closing_balance"])))
                if metadata.get("closing_balance") not in (None, "")
                else None
            ),
            "declared_transaction_count": metadata.get("declared_transaction_count"),
            "total_amount_due": metadata.get("total_amount_due") or None,
            "minimum_amount_due": metadata.get("minimum_amount_due") or None,
            "payment_due_date": metadata.get("payment_due_date") or None,
            "credit_limit": metadata.get("credit_limit") or None,
        },
        "reconciliation": {
            "total_debits": str(debits),
            "total_credits": str(credits),
            "expected_closing": (
                str(expected_closing) if expected_closing is not None else None
            ),
        },
        "transactions": [
            {
                "txn_date": row["txn_date"],
                "value_date": optional(row, "value_date") or row["txn_date"],
                "description": row["description"],
                "amount": str(Decimal(str(row["amount"]))),
                "direction": row["direction"],
                "balance_after": (
                    str(Decimal(str(row["balance_after"])))
                    if row.get("balance_after") not in (None, "")
                    else None
                ),
                **{field: optional(row, field) for field in OPTIONAL_ROW_FIELDS
                   if field not in ("value_date", "balance_after")},
            }
            for row in rows
        ],
    }


def write(payload: dict[str, Any], destination: Path) -> Path:
    destination.write_text(json.dumps(payload, indent=2) + "\n")
    return destination
