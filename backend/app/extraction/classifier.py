"""Bank statement vs credit-card statement.

Keyword scoring, not a model. The words that separate the two are printed by
the issuer's core system — "Minimum Amount Due" appears on every card statement
and no savings statement — so a lookup table is both more accurate here and
auditable in a way a classifier's weights are not.

The distinction matters more than it looks. A card statement fed to a bank-
statement parser has its summary block read as transactions, and its refunds
read as income. Getting the document type wrong is how a parser produces a
confidently wrong ledger instead of an obviously empty one.
"""

from __future__ import annotations

from app.models.enums import DocumentType

_CARD_MARKERS: tuple[str, ...] = (
    "minimum amount due", "total amount due", "payment due date",
    "credit card statement", "statement of account for card",
    "available credit limit", "credit limit", "reward points", "card number",
    "billing cycle", "previous balance", "purchases & other charges",
    "payments & credits", "finance charges",
)

_BANK_MARKERS: tuple[str, ...] = (
    "account statement", "statement of account", "opening balance",
    "closing balance", "ifsc", "branch", "withdrawal", "deposit",
    "account number", "value date", "cheque", "balance as on",
    "detailed statement", "running balance",
)


def classify_document(sample_text: str) -> tuple[DocumentType, float]:
    lowered = sample_text.lower()
    card_hits = sum(1 for marker in _CARD_MARKERS if marker in lowered)
    bank_hits = sum(1 for marker in _BANK_MARKERS if marker in lowered)

    if card_hits == 0 and bank_hits == 0:
        return DocumentType.UNKNOWN, 0.0

    total = card_hits + bank_hits
    if card_hits > bank_hits:
        return DocumentType.CREDIT_CARD_STATEMENT, round(card_hits / total, 3)
    if bank_hits > card_hits:
        return DocumentType.BANK_STATEMENT, round(bank_hits / total, 3)

    # A genuine tie. A card statement issued by a bank carries both
    # vocabularies, so say "unknown" rather than guessing — the registry can
    # still dispatch on bank signature alone.
    return DocumentType.UNKNOWN, 0.5
