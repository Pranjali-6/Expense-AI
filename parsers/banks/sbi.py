"""State Bank of India account statements.

Layout: ``Txn Date | Value Date | Description | Ref No./Cheque No. | Debit |
Credit | Balance``, dates as ``01 Apr 2024``.

SBI narrations lead with a direction word — ``TO TRANSFER-``, ``BY TRANSFER-``,
``BY CASH WDL-`` — which is genuinely useful corroboration: it agrees with the
Debit/Credit column, and when the two disagree the row deserves review rather
than a silent choice between them.
"""

from __future__ import annotations

import re

from app.models.enums import Direction

from parsers.banks.generic import TabularBankParser
from parsers.canonical import ParseResult
from parsers.document import ExtractedDocument
from parsers.registry import registry

# Only the prefixes that genuinely carry direction. SBI's `BY` is not a credit
# marker on its own — "BY DEBIT CARD-OTHPG …" and "BY CASH WDL-ATM …" are both
# withdrawals, where `BY` introduces the *instrument*, not the direction. Reading
# `BY` as "money in" flags a third of a real statement as contradictory, which
# is how a useful cross-check becomes noise a reviewer learns to ignore.
_DEBIT_PREFIX = re.compile(
    r"^\s*(?:TO\s+TRANSFER|TO\s+ATM|TO\s+CHEQUE|BY\s+CASH\s+WDL|BY\s+DEBIT\s+CARD)",
    re.IGNORECASE,
)
_CREDIT_PREFIX = re.compile(
    r"^\s*(?:BY\s+TRANSFER|BY\s+CASH\s+DEP|BY\s+CLEARING)", re.IGNORECASE
)


class SBIBankParser(TabularBankParser):
    bank_code = "SBI"
    bank_name = "State Bank of India"
    parser_name = "sbi_bank"
    parser_version = "1.0"
    priority = 100

    signatures = (
        "state bank of india",
        "account statement",
    )
    ifsc_prefixes = ("SBIN",)

    def parse(self, document: ExtractedDocument) -> ParseResult:
        result = super().parse(document)

        # `TO ...` is money leaving, `BY ...` is money arriving. Where the
        # narration contradicts the column the row is flagged, not corrected:
        # the column is the better evidence, but a disagreement means one of the
        # two was misread and a human should look.
        conflicts = 0
        for transaction in result.transactions:
            expected: Direction | None = None
            if _DEBIT_PREFIX.match(transaction.description):
                expected = Direction.DEBIT
            elif _CREDIT_PREFIX.match(transaction.description):
                expected = Direction.CREDIT

            if expected is None:
                continue
            if transaction.direction != expected:
                conflicts += 1
                transaction.field_confidence["direction"] = 0.55
            else:
                transaction.field_confidence["direction"] = 1.0

        if conflicts:
            result.warnings.append("narration_direction_conflict")

        return result


registry.register(SBIBankParser())
