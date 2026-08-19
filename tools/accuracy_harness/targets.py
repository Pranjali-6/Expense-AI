"""The published accuracy targets, and the gate that enforces them.

These are the numbers the platform claims. They are checked on every run and a
miss exits non-zero, so the claim cannot quietly drift away from the code.

Reconciliation is not a percentage and has no tolerance band. A statement whose
arithmetic does not close to exactly ₹0.00 has not been read correctly, and
"99.8% of statements reconciled" is not a meaningful thing to say about
somebody's money.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Target:
    key: str
    label: str
    minimum: float


TARGETS: tuple[Target, ...] = (
    Target("recall", "Transaction recall", 0.99),
    Target("precision", "Transaction precision", 0.995),
    Target("date_accuracy", "Date accuracy", 0.995),
    Target("amount_accuracy", "Amount accuracy", 0.999),
    Target("direction_accuracy", "Debit/Credit direction", 0.999),
    Target("merchant_accuracy", "Merchant normalization", 0.98),
    Target("category_accuracy", "Category assignment", 0.95),
)

#: Reconciliation is a count, not a rate: every statement must reconcile.
RECONCILIATION_MUST_BE_TOTAL = True
