"""The parser contract.

A parser answers two questions: *is this mine?* and *what does it say?* Keeping
detection on the parser rather than in a central lookup means adding a bank is
one self-contained file — the thing that decides an HDFC statement is HDFC is
the same code that knows how to read one.

Every parser returns a :class:`~parsers.canonical.ParseResult`, so nothing
downstream ever branches on which bank a statement came from.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from datetime import date
from decimal import Decimal

from app.models.enums import CategorySource, Direction, DocumentType, MovementType

from parsers.canonical import CanonicalTransaction, ParseResult, StatementMetadata
from parsers.document import ExtractedDocument
from parsers.merchants.normalizer import normalize_merchant
from parsers.merchants.rules import match_rule
from parsers.normalizers import dates as datenorm


class BankParser(ABC):
    """Base class for every statement parser."""

    #: Stable identifier, stored on the statement row.
    bank_code: str = "UNKNOWN"
    bank_name: str = "Unknown"
    #: Parser identity, versioned separately from the bank so a parser fix is
    #: visible in the accuracy history without inventing a new bank.
    parser_name: str = "base"
    parser_version: str = "1.0"
    #: What this parser reads. A card parser must not claim a bank statement.
    document_types: tuple[DocumentType, ...] = (DocumentType.BANK_STATEMENT,)
    #: Tried in descending order; the generic fallbacks sit at the bottom.
    priority: int = 100

    # ------------------------------------------------------------- detection --

    #: Phrases that identify the issuer. Matched case-insensitively against the
    #: first pages. Deliberately phrases printed by the bank, not customer data.
    signatures: tuple[str, ...] = ()
    #: IFSC prefixes belonging to this bank, e.g. ("HDFC",).
    ifsc_prefixes: tuple[str, ...] = ()

    def detect(self, document: ExtractedDocument) -> float:
        """Confidence in the range 0–1 that this parser owns the document.

        Signature phrases are worth more than an IFSC prefix because an IFSC
        can appear in a beneficiary narration on someone else's statement — a
        payment *to* an HDFC account is not an HDFC statement.
        """
        # The masthead, never the full header text — see ExtractedDocument.masthead.
        masthead = document.masthead()
        header = masthead.lower()
        if not header:
            return 0.0

        hits = sum(1 for phrase in self.signatures if phrase.lower() in header)
        score = min(hits / max(len(self.signatures), 1), 1.0) * 0.8 if self.signatures else 0.0

        if self.ifsc_prefixes:
            upper = masthead.upper()
            for prefix in self.ifsc_prefixes:
                if re.search(rf"\b{re.escape(prefix)}0[A-Z0-9]{{6}}\b", upper):
                    score += 0.2
                    break

        return round(min(score, 1.0), 3)

    # ----------------------------------------------------------------- parse --

    @abstractmethod
    def parse(self, document: ExtractedDocument) -> ParseResult:
        """Read the document into the canonical schema."""

    # ------------------------------------------------------------- utilities --

    def _result(self, metadata: StatementMetadata) -> ParseResult:
        metadata.bank_code = metadata.bank_code or self.bank_code
        metadata.bank_name = metadata.bank_name or self.bank_name
        return ParseResult(
            metadata=metadata,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
        )

    @staticmethod
    def enrich(transactions: list[CanonicalTransaction]) -> None:
        """Attach merchant, payment method and category in place.

        Runs once per statement after parsing rather than inside each parser, so
        every bank gets identical treatment. A per-bank merchant implementation
        is how the same shop ends up with two names.

        Order matters. The deterministic rule runs *after* the merchant lookup
        but *outranks* it on category, because structure beats identity: a
        refund of an Amazon purchase has Amazon as its merchant and Refund as
        its category, and a rule that only fired when the dictionary missed
        would file it under Shopping.
        """
        for txn in transactions:
            match = normalize_merchant(txn.description)
            rule = match_rule(txn.description)

            txn.merchant_raw = txn.description
            txn.payment_method = match.payment_method
            txn.field_confidence["merchant"] = match.confidence

            # Rows with no merchant: an ATM withdrawal, a service charge, an
            # interest credit. Emitting leftover words here would create junk
            # merchants in analytics and hand the privacy gateway an unverified
            # string to guard against.
            if rule is not None and rule.rule.suppress_merchant:
                txn.merchant_normalized = None
                txn.merchant_slug = None
                # A confident *absence*: this rail has no merchant, and
                # saying so is a correct answer rather than a weak one.
                txn.field_confidence["merchant"] = 0.98
            else:
                txn.merchant_normalized = match.name
                txn.merchant_slug = match.slug

            if match.category_slug:
                txn.category_slug = match.category_slug
                txn.subcategory_slug = match.subcategory_slug
                txn.category_source = CategorySource.VERIFIED_MERCHANT_RULE
                txn.category_reason = {
                    "tier": "verified_merchant_rule",
                    "merchant_slug": match.slug,
                    **match.evidence,
                }

            if rule is not None:
                txn.movement_type = rule.rule.movement
                txn.is_expense = rule.rule.is_expense
                if rule.rule.overrides_merchant_category or not txn.category_slug:
                    txn.category_slug = rule.rule.category_slug
                    txn.subcategory_slug = rule.rule.subcategory_slug
                    txn.category_source = CategorySource.DETERMINISTIC_RULE
                    txn.category_reason = {
                        "tier": "deterministic_rule",
                        "rule": rule.rule.code,
                        "matched": rule.matched_text.strip().lower(),
                    }
            else:
                # No structural rule fired, so this is ordinary activity, and
                # direction settles what kind. Leaving it `unknown` would be
                # both wrong and load-bearing: `is_expense` defaults to true, so
                # an unclassified *credit* would be counted as spending, and
                # analytics that filter on movement type would see a category
                # that means "we did not look".
                is_debit = txn.direction == Direction.DEBIT
                txn.movement_type = MovementType.EXPENSE if is_debit else MovementType.INCOME
                txn.is_expense = is_debit
                if txn.category_slug is None:
                    txn.category_source = CategorySource.FALLBACK_OTHER

    @staticmethod
    def period_year(metadata: StatementMetadata) -> int | None:
        """Year to use for date cells that omit one."""
        anchor = metadata.period_end or metadata.period_start
        return anchor.year if anchor else None

    @staticmethod
    def find_period(text: str) -> tuple[date | None, date | None]:
        """Pull a statement period out of header text.

        Handles the three shapes Indian banks print: an explicit ``From … To …``,
        a bare ``dd/mm/yyyy - dd/mm/yyyy`` range, and a ``Statement Period``
        label followed by either.
        """
        patterns = (
            r"(?:from|period\s*(?:from)?)\s*[:\-]?\s*([0-9A-Za-z/\-. ]{6,20}?)\s*"
            r"(?:to|-|–|through)\s*([0-9A-Za-z/\-. ]{6,20})",
            r"statement\s+period\s*[:\-]?\s*([0-9A-Za-z/\-. ]{6,20}?)\s*"
            r"(?:to|-|–)\s*([0-9A-Za-z/\-. ]{6,20})",
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if not match:
                continue
            try:
                return (
                    datenorm.parse_date(match.group(1)),
                    datenorm.parse_date(match.group(2)),
                )
            except datenorm.DateParseError:
                continue
        return None, None

    @staticmethod
    def find_last4(text: str) -> str | None:
        """Last four digits of the account or card this statement belongs to.

        Only ever the last four. A parser that captured a full account number
        would be creating the exact identifier the rest of the system spends its
        time making sure never gets stored or transmitted.
        """
        patterns = (
            r"(?:account|a/c|acct|card)\s*(?:number|no\.?|#)?\s*[:\-]?\s*"
            r"([0-9Xx*]{6,20})",
            r"\b[0-9]{0,6}[Xx*]{4,12}([0-9]{4})\b",
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                digits = re.sub(r"\D", "", match.group(1))
                if len(digits) >= 4:
                    return digits[-4:]
        return None

    @staticmethod
    def find_money_label(text: str, *labels: str) -> Decimal | None:
        """Find a labelled money value such as ``Closing Balance : 1,23,456.78``."""
        from parsers.normalizers.amount import AmountParseError, parse_amount

        for label in labels:
            # A minus *attached* to the digits is a sign; a minus surrounded by
            # spaces is a separator ("Balance - 1,234.56"). Distinguishing them
            # matters: the earlier pattern let `[:\-]?` swallow the sign, so an
            # overdrawn account's closing balance of -11,262.43 was read as
            # +11,262.43 — and the statement then failed to reconcile by exactly
            # twice the balance, with nothing to indicate why.
            match = re.search(
                rf"{label}\s*(?:\(inr\)|\(rs\.?\))?\s*(?::|=|\s-\s)?\s*"
                r"((?:rs\.?|inr|₹)?\s*-?[\d,]+(?:\.\d{1,2})?\s*(?:dr|cr)?)",
                text,
                re.IGNORECASE,
            )
            if match:
                try:
                    return parse_amount(match.group(1))
                except AmountParseError:
                    continue
        return None
