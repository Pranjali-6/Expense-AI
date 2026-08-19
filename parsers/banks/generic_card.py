"""Credit-card statement parsing.

A card statement is a different financial object from a bank statement, and
treating it as one is a rich source of wrong numbers:

* **There is no running balance.** Direction cannot be recovered from a balance
  delta, so the only evidence is the ``Cr`` suffix — and most issuers print
  purchases with no suffix at all. An unmarked row is therefore a *debit* by
  convention, declared explicitly here rather than guessed per row.
* **Refunds are credits, not income.** A ₹4,200 refund read as a debit does not
  merely lose ₹4,200, it adds ₹8,400 of error to the month.
* **The arithmetic is different.** A card reconciles as
  ``previous balance + purchases − payments/credits = total amount due``, with
  no per-row balance to corroborate it. That single identity is the only
  arithmetic check available, which is why it is checked exactly.

Card payments are also the classic double-count: the payment appears as a credit
here *and* as a debit on the bank statement that funded it, while the purchases
it settles are already counted individually. Movement classification in P5 marks
both sides; this parser's job is only to read them correctly.
"""

from __future__ import annotations

import re
from decimal import Decimal

from app.models.enums import Direction, DocumentType

from parsers.banks.generic import TabularBankParser
from parsers.canonical import ParseResult, StatementMetadata
from parsers.document import ExtractedDocument
from parsers.normalizers import dates as datenorm
from parsers.registry import registry


class CreditCardParser(TabularBankParser):
    bank_code = "GENERIC"
    bank_name = "Unidentified card issuer"
    parser_name = "generic_card"
    parser_version = "1.0"
    document_types = (DocumentType.CREDIT_CARD_STATEMENT,)
    priority = 0

    # A card row with no Dr/Cr marker is a purchase.
    single_column_default = Direction.DEBIT

    def read_metadata(self, document: ExtractedDocument) -> StatementMetadata:
        # The summary block is often on the last page, so the whole document is
        # searched rather than just the header.
        text = document.full_text
        period_start, period_end = self.find_period(text)

        metadata = StatementMetadata(
            bank_code=self.bank_code,
            bank_name=self.bank_name,
            document_type=DocumentType.CREDIT_CARD_STATEMENT,
            account_last4=self.find_last4(text),
            account_type="credit_card",
            period_start=period_start,
            period_end=period_end,
            opening_balance=self.find_money_label(
                text, r"previous\s+balance", r"opening\s+balance",
                r"previous\s+statement\s+balance",
            ),
            closing_balance=self.find_money_label(
                text, r"total\s+amount\s+due", r"total\s+dues", r"closing\s+balance",
            ),
        )
        metadata.total_amount_due = metadata.closing_balance
        metadata.minimum_amount_due = self.find_money_label(
            text, r"minimum\s+amount\s+due", r"min(?:imum)?\s+due",
        )
        metadata.credit_limit = self.find_money_label(
            text, r"credit\s+limit", r"total\s+credit\s+limit",
        )

        due = re.search(
            r"payment\s+due\s+date\s*[:\-]?\s*([0-9A-Za-z/\-. ]{6,20})", text, re.IGNORECASE
        )
        if due:
            try:
                metadata.payment_due_date = datenorm.parse_date(due.group(1))
            except datenorm.DateParseError:
                pass

        return metadata

    def parse(self, document: ExtractedDocument) -> ParseResult:
        result = super().parse(document)

        # A card statement has no per-row balance, so the generic parser's
        # "derive the opening balance from the first row" path cannot apply.
        # Drop anything it inferred rather than let a fabricated previous
        # balance make the reconciliation identity appear to hold.
        for warning in ("opening_balance_derived", "closing_balance_derived"):
            if warning in result.warnings:
                result.warnings.remove(warning)
        if "opening_balance_derived" in result.warnings:
            result.metadata.opening_balance = None

        for transaction in result.transactions:
            transaction.balance_after = None

        return result

    @staticmethod
    def reconcile(metadata: StatementMetadata, transactions) -> Decimal | None:
        """``previous + purchases − credits − total due``. Zero means it balances.

        Returns ``None`` when the statement did not print enough to check —
        which is reported as "not verified", never as "verified".
        """
        if metadata.opening_balance is None or metadata.total_amount_due is None:
            return None

        purchases = sum(
            (txn.amount for txn in transactions if txn.direction == Direction.DEBIT),
            Decimal("0.00"),
        )
        credits = sum(
            (txn.amount for txn in transactions if txn.direction == Direction.CREDIT),
            Decimal("0.00"),
        )
        return (
            metadata.opening_balance + purchases - credits - metadata.total_amount_due
        ).quantize(Decimal("0.01"))


class HDFCCardParser(CreditCardParser):
    bank_code = "HDFC"
    bank_name = "HDFC Bank Credit Card"
    parser_name = "hdfc_card"
    priority = 100
    signatures = ("hdfc bank limited", "credit card statement")


class ICICICardParser(CreditCardParser):
    bank_code = "ICICI"
    bank_name = "ICICI Bank Credit Card"
    parser_name = "icici_card"
    priority = 100
    signatures = ("icici bank limited", "credit card statement")


registry.register(CreditCardParser())
registry.register(HDFCCardParser())
registry.register(ICICICardParser())
