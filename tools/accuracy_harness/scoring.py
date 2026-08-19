"""Transaction-level accuracy scoring.

The rule this module exists to enforce:

    **The denominator for every field metric is the ground-truth transaction
    count, and a missing transaction counts as a failure in every field
    metric.**

A harness that scores only the rows it managed to extract can report 99.9%
amount accuracy while silently dropping 40% of a statement. That number would be
a lie, and this scorer is built so it cannot be told. Recall and precision are
reported separately and never averaged into a single headline figure.

Alignment is two-stage, which matters for honesty in the other direction too. If
rows were aligned only on an exact key, a transaction read with the wrong date
would count as one *missing* row plus one *extra* row — inflating both failures
and hiding the fact that the real defect was a date. So a relaxed pass pairs
leftover rows by description similarity and near-miss amounts, and the resulting
pair is scored field by field. A row that finds no partner at all is genuinely
missing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Iterable

from app.models.enums import Direction

from parsers.canonical import CanonicalTransaction

# A relaxed pair must be this similar in description *and* agree on either the
# amount or the date. Two unrelated ₹500 UPI payments on the same day would
# otherwise pair up and hide a genuine miss.
_DESCRIPTION_FLOOR = 0.72
_DATE_SLACK_DAYS = 4


@dataclass(slots=True)
class GroundTruth:
    txn_date: date
    description: str
    amount: Decimal
    direction: Direction
    merchant: str | None
    category_slug: str | None
    balance_after: Decimal | None

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> GroundTruth:
        return cls(
            txn_date=date.fromisoformat(raw["txn_date"]),
            description=raw["description"],
            amount=Decimal(raw["amount"]),
            direction=Direction(raw["direction"]),
            merchant=raw.get("merchant_normalized"),
            category_slug=raw.get("category_slug"),
            balance_after=(
                Decimal(raw["balance_after"]) if raw.get("balance_after") else None
            ),
        )


@dataclass(slots=True)
class FieldErrors:
    date: int = 0
    amount: int = 0
    direction: int = 0
    merchant: int = 0
    category: int = 0


@dataclass(slots=True)
class Scorecard:
    fixture: str
    bank_code: str
    document_type: str

    expected_count: int = 0
    extracted_count: int = 0
    matched_count: int = 0
    missing: int = 0
    extra: int = 0

    errors: FieldErrors = field(default_factory=FieldErrors)

    reconciles: bool = False
    reconciliation_delta: Decimal | None = None
    reconciliation_checked: bool = False

    # First few concrete failures, for a message a human can act on. Carries
    # row positions and field names — never amounts or descriptions, which are
    # forbidden from logs and from anything that might reach one.
    examples: list[str] = field(default_factory=list)

    # ------------------------------------------------------------- rates ----
    # Every one divides by `expected_count`. That is the whole point.

    def _rate(self, wrong: int) -> float:
        if self.expected_count == 0:
            return 0.0
        return max(0.0, (self.expected_count - wrong) / self.expected_count)

    @property
    def recall(self) -> float:
        return self._rate(self.missing)

    @property
    def precision(self) -> float:
        if self.extracted_count == 0:
            return 0.0
        return max(0.0, (self.extracted_count - self.extra) / self.extracted_count)

    @property
    def date_accuracy(self) -> float:
        return self._rate(self.errors.date + self.missing)

    @property
    def amount_accuracy(self) -> float:
        return self._rate(self.errors.amount + self.missing)

    @property
    def direction_accuracy(self) -> float:
        return self._rate(self.errors.direction + self.missing)

    @property
    def merchant_accuracy(self) -> float:
        return self._rate(self.errors.merchant + self.missing)

    @property
    def category_accuracy(self) -> float:
        return self._rate(self.errors.category + self.missing)

    def as_dict(self) -> dict[str, Any]:
        return {
            "fixture": self.fixture,
            "bank_code": self.bank_code,
            "document_type": self.document_type,
            "expected_count": self.expected_count,
            "extracted_count": self.extracted_count,
            "matched_count": self.matched_count,
            "missing_transactions": self.missing,
            "extra_transactions": self.extra,
            "wrong_date": self.errors.date,
            "wrong_amount": self.errors.amount,
            "wrong_direction": self.errors.direction,
            "wrong_merchant": self.errors.merchant,
            "wrong_category": self.errors.category,
            "recall": round(self.recall, 5),
            "precision": round(self.precision, 5),
            "date_accuracy": round(self.date_accuracy, 5),
            "amount_accuracy": round(self.amount_accuracy, 5),
            "direction_accuracy": round(self.direction_accuracy, 5),
            "merchant_accuracy": round(self.merchant_accuracy, 5),
            "category_accuracy": round(self.category_accuracy, 5),
            "reconciles": self.reconciles,
            "reconciliation_checked": self.reconciliation_checked,
            "reconciliation_delta": (
                str(self.reconciliation_delta)
                if self.reconciliation_delta is not None else None
            ),
        }


def _similarity(left: str, right: str) -> float:
    from rapidfuzz import fuzz

    return fuzz.token_set_ratio(left.upper(), right.upper()) / 100.0


def _exact_key(txn_date: date, amount: Decimal, direction: Direction) -> tuple:
    return (txn_date, amount, str(direction))


def align(
    expected: list[GroundTruth], extracted: list[CanonicalTransaction]
) -> tuple[list[tuple[GroundTruth, CanonicalTransaction]], list[GroundTruth], list[CanonicalTransaction]]:
    """Pair ground-truth rows with extracted rows.

    Returns ``(pairs, missing, extra)``.
    """
    remaining = list(extracted)
    pairs: list[tuple[GroundTruth, CanonicalTransaction]] = []
    unpaired: list[GroundTruth] = []

    # Pass 1 — exact on (date, amount, direction), then break ties on the most
    # similar description. Statements genuinely contain two identical-looking
    # rows on the same day; pairing them arbitrarily would misattribute their
    # descriptions and manufacture merchant errors.
    buckets: dict[tuple, list[CanonicalTransaction]] = {}
    for txn in remaining:
        buckets.setdefault(_exact_key(txn.txn_date, txn.amount, txn.direction), []).append(txn)

    for truth in expected:
        key = _exact_key(truth.txn_date, truth.amount, truth.direction)
        candidates = buckets.get(key)
        if not candidates:
            unpaired.append(truth)
            continue
        best = max(candidates, key=lambda txn: _similarity(truth.description, txn.description))
        candidates.remove(best)
        remaining.remove(best)
        pairs.append((truth, best))

    # Pass 2 — relaxed. Anything left is a *defect*, not a miss, if a plausible
    # partner exists: it was read, just read wrong.
    still_unpaired: list[GroundTruth] = []
    for truth in unpaired:
        best: CanonicalTransaction | None = None
        best_score = 0.0
        for txn in remaining:
            score = _similarity(truth.description, txn.description)
            if score < _DESCRIPTION_FLOOR:
                continue
            close_amount = txn.amount == truth.amount
            close_date = abs((txn.txn_date - truth.txn_date).days) <= _DATE_SLACK_DAYS
            if not (close_amount or close_date):
                continue
            # Prefer the closest date among equally-worded candidates.
            score += 0.05 if close_amount else 0.0
            score -= min(abs((txn.txn_date - truth.txn_date).days), 30) * 0.001
            if score > best_score:
                best, best_score = txn, score

        if best is None:
            still_unpaired.append(truth)
        else:
            remaining.remove(best)
            pairs.append((truth, best))

    return pairs, still_unpaired, remaining


def _same_merchant(expected: str | None, actual: str | None) -> bool:
    if expected is None and actual is None:
        return True
    if expected is None or actual is None:
        return False
    return expected.strip().casefold() == actual.strip().casefold()


def score(
    *,
    fixture: str,
    bank_code: str,
    document_type: str,
    expected: list[GroundTruth],
    extracted: list[CanonicalTransaction],
) -> Scorecard:
    card = Scorecard(fixture=fixture, bank_code=bank_code, document_type=document_type)
    card.expected_count = len(expected)
    card.extracted_count = len(extracted)

    pairs, missing, extra = align(expected, extracted)
    card.matched_count = len(pairs)
    card.missing = len(missing)
    card.extra = len(extra)

    for truth, actual in pairs:
        if actual.txn_date != truth.txn_date:
            card.errors.date += 1
            card.examples.append(f"wrong_date at page {actual.source_page} row {actual.source_row}")
        if actual.amount != truth.amount:
            card.errors.amount += 1
            card.examples.append(f"wrong_amount at page {actual.source_page} row {actual.source_row}")
        if actual.direction != truth.direction:
            card.errors.direction += 1
            card.examples.append(
                f"wrong_direction at page {actual.source_page} row {actual.source_row}"
            )
        if not _same_merchant(truth.merchant, actual.merchant_normalized):
            card.errors.merchant += 1
        if (truth.category_slug or None) != (actual.category_slug or None):
            card.errors.category += 1

    for truth in missing:
        card.examples.append(f"missing transaction dated {truth.txn_date.isoformat()}")
    for actual in extra:
        card.examples.append(
            f"extra transaction at page {actual.source_page} row {actual.source_row}"
        )

    card.examples = card.examples[:8]
    return card


def reconcile_bank(
    opening: Decimal | None,
    closing: Decimal | None,
    transactions: Iterable[CanonicalTransaction],
) -> tuple[bool, Decimal | None]:
    """``opening − debits + credits − closing``. Exactly zero, or it fails.

    ``None`` means the statement did not print enough to check. That is reported
    as unverified, never as verified — the difference between "the arithmetic
    holds" and "nobody checked" is the whole basis of the trust status.
    """
    if opening is None or closing is None:
        return False, None

    debits = sum(
        (txn.amount for txn in transactions if txn.direction == Direction.DEBIT),
        Decimal("0.00"),
    )
    credits = sum(
        (txn.amount for txn in transactions if txn.direction == Direction.CREDIT),
        Decimal("0.00"),
    )
    delta = (opening - debits + credits - closing).quantize(Decimal("0.01"))
    return delta == 0, delta


def reconcile_card(
    previous: Decimal | None,
    total_due: Decimal | None,
    transactions: Iterable[CanonicalTransaction],
) -> tuple[bool, Decimal | None]:
    """``previous + purchases − credits − total due``, exactly zero."""
    if previous is None or total_due is None:
        return False, None

    purchases = sum(
        (txn.amount for txn in transactions if txn.direction == Direction.DEBIT),
        Decimal("0.00"),
    )
    credits = sum(
        (txn.amount for txn in transactions if txn.direction == Direction.CREDIT),
        Decimal("0.00"),
    )
    delta = (previous + purchases - credits - total_due).quantize(Decimal("0.01"))
    return delta == 0, delta
