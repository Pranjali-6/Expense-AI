"""Four confidence dimensions, gated on the minimum.

A single blended score hides the failure that matters. A transaction whose
category is certain but whose amount was misread by OCR averages out to
"probably fine" and lands in the ledger; the same transaction scored on four
axes reports ``extraction 0.5`` and goes to review. So the gate is **``min()``,
never a mean** — and it is a generated column in PostgreSQL (``LEAST(...)``), so
the definition cannot drift away from the code that reads it.

| dimension | asks |
|---|---|
| ``extraction`` | did we read the row correctly? |
| ``merchant`` | did we identify who it was? |
| ``category`` | is the category right? |
| ``validation`` | does the statement it came from hang together? |

The bands are chosen so that "flagged" still counts toward totals — a flagged
transaction is in the ledger and in the arithmetic, it just carries a visible
mark. Only ``review_required`` demands a human before the row is treated as
settled, and even then the row is stored, because a statement that reconciles
must reconcile with every one of its transactions present.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.models.enums import CategorySource, ReviewStatus

from parsers.canonical import CanonicalTransaction

AUTO_APPROVE_AT = Decimal("0.97")
FLAG_AT = Decimal("0.90")

#: How much certainty each cascade tier carries. A deterministic narration rule
#: is worth nearly as much as a dictionary hit because both are auditable and
#: reproducible; the fallback is deliberately low enough to force review, since
#: an uncategorised transaction is precisely what a person should look at.
_CATEGORY_TIER: dict[CategorySource, Decimal] = {
    CategorySource.USER_RULE: Decimal("1.00"),
    CategorySource.VERIFIED_MERCHANT_RULE: Decimal("0.98"),
    CategorySource.DETERMINISTIC_RULE: Decimal("0.97"),
    CategorySource.HISTORICAL_PATTERN: Decimal("0.90"),
    CategorySource.AI_MODEL: Decimal("0.85"),
    CategorySource.FALLBACK_OTHER: Decimal("0.60"),
}

#: OCR read a row rather than a text layer. Tesseract misreads digits, so every
#: OCR-sourced row is discounted even when nothing looks wrong — "nothing looks
#: wrong" is exactly what a confident misread looks like.
_OCR_PENALTY = Decimal("0.92")


@dataclass(slots=True)
class Confidence:
    extraction: Decimal
    merchant: Decimal
    category: Decimal
    validation: Decimal

    @property
    def minimum(self) -> Decimal:
        return min(self.extraction, self.merchant, self.category, self.validation)

    @property
    def review_status(self) -> ReviewStatus:
        value = self.minimum
        if value >= AUTO_APPROVE_AT:
            return ReviewStatus.AUTO_APPROVED
        if value >= FLAG_AT:
            return ReviewStatus.FLAGGED
        return ReviewStatus.REVIEW_REQUIRED

    @property
    def weakest(self) -> str:
        """Which dimension is dragging the row down — what a reviewer needs."""
        pairs = (
            ("extraction", self.extraction), ("merchant", self.merchant),
            ("category", self.category), ("validation", self.validation),
        )
        return min(pairs, key=lambda pair: pair[1])[0]


def _clamp(value: Decimal) -> Decimal:
    return max(Decimal("0.00"), min(Decimal("1.00"), value)).quantize(Decimal("0.0001"))


def statement_validation_score(
    *,
    reconciles: bool,
    unverifiable: bool,
    balance_checked: bool,
    balance_continuous: bool,
    pages_continuous: bool,
) -> Decimal:
    """One score for every transaction on a statement.

    Validation is a property of the document, not of the row: if the statement
    does not add up, no individual transaction on it can be fully trusted, even
    the ones that look immaculate — the misread might be *this* row.
    """
    if unverifiable:
        # The statement did not print both balances. Nothing is wrong; nothing
        # is confirmed either. Deliberately below the auto-approve line: an
        # unchecked statement must not enter the ledger as settled fact.
        score = Decimal("0.80")
    elif not reconciles:
        score = Decimal("0.55")
    elif balance_checked and balance_continuous:
        score = Decimal("0.99")
    elif balance_checked and not balance_continuous:
        # Totals close but the running balance wanders: two errors that cancel.
        score = Decimal("0.70")
    else:
        # Reconciles exactly, no per-row balance to corroborate it — the normal
        # case for a credit-card statement.
        score = Decimal("0.97")

    if not pages_continuous:
        score = min(score, Decimal("0.70"))

    return _clamp(score)


def score(
    transaction: CanonicalTransaction,
    *,
    validation: Decimal,
    from_ocr_page: bool = False,
    suspected_duplicate: bool = False,
    out_of_period: bool = False,
) -> Confidence:
    fields = transaction.field_confidence or {}

    extraction = Decimal(
        str(min(
            (fields.get(key, 1.0) for key in ("date", "amount", "direction")),
            default=1.0,
        ))
    )
    if from_ocr_page:
        extraction *= _OCR_PENALTY

    merchant = Decimal(str(fields.get("merchant", 0.5)))

    category = _CATEGORY_TIER.get(transaction.category_source, Decimal("0.60"))
    if transaction.category_source == CategorySource.VERIFIED_MERCHANT_RULE:
        # The category is only as good as the merchant match it came from.
        category = min(category, merchant)
    if transaction.category_slug is None:
        category = min(category, Decimal("0.60"))

    if suspected_duplicate:
        # Not a reading error — a question about whether this row should exist
        # at all, which only a person can settle.
        validation = min(validation, Decimal("0.60"))
    if out_of_period:
        validation = min(validation, Decimal("0.85"))

    return Confidence(
        extraction=_clamp(extraction),
        merchant=_clamp(merchant),
        category=_clamp(category),
        validation=_clamp(validation),
    )
