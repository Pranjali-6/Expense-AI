"""HDFC Bank savings and current account statements.

Layout: ``Date | Narration | Chq./Ref.No. | Value Dt | Withdrawal Amt. |
Deposit Amt. | Closing Balance``, dates as ``dd/mm/yy``.

The two-digit year is the thing worth naming. ``01/03/24`` is unambiguous only
because the parser is day-first everywhere; read month-first it becomes 3
January and the transaction silently moves two months. The base date parser
refuses to be locale-dependent, so this parser needs no special handling — but
the fixture set contains this layout precisely so that guarantee is tested.
"""

from __future__ import annotations

from parsers.banks.generic import TabularBankParser
from parsers.registry import registry


class HDFCBankParser(TabularBankParser):
    bank_code = "HDFC"
    bank_name = "HDFC Bank"
    parser_name = "hdfc_bank"
    parser_version = "1.0"
    priority = 100

    signatures = (
        "hdfc bank limited",
        "hdfc bank ltd",
        "statement of account",
    )
    ifsc_prefixes = ("HDFC",)


registry.register(HDFCBankParser())
