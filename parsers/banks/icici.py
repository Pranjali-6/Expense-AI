"""ICICI Bank account statements.

Layout: ``S No. | Value Date | Transaction Date | Cheque Number |
Transaction Remarks | Withdrawal Amount (INR) | Deposit Amount (INR) |
Balance (INR)``.

Note the column order: **value date comes before transaction date**. A parser
that assumes the first date-shaped column is the transaction date reads the
whole statement one or two days off, which is invisible on a monthly total and
very visible on a daily chart. The base parser maps by header text rather than
position, which is what makes this survivable.
"""

from __future__ import annotations

from parsers.banks.generic import TabularBankParser
from parsers.registry import registry


class ICICIBankParser(TabularBankParser):
    bank_code = "ICICI"
    bank_name = "ICICI Bank"
    parser_name = "icici_bank"
    parser_version = "1.0"
    priority = 100

    signatures = (
        "icici bank limited",
        "detailed statement",
        "transaction remarks",
    )
    ifsc_prefixes = ("ICIC",)


registry.register(ICICIBankParser())
