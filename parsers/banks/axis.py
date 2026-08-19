"""Axis Bank account statements.

Layout: ``Tran Date | Chq No | Particulars | Debit | Credit | Balance |
Init.Br``.

The trailing branch column is the quirk: it is short, often blank, and sits
where a naive right-to-left "the last number is the balance" heuristic expects
the balance to be. Mapping columns by header text rather than by position is
what keeps that from mattering, and the multi-page Axis fixture exists to prove
the mapping survives page breaks where the header is not reprinted.
"""

from __future__ import annotations

from parsers.banks.generic import TabularBankParser
from parsers.registry import registry


class AxisBankParser(TabularBankParser):
    bank_code = "AXIS"
    bank_name = "Axis Bank"
    parser_name = "axis_bank"
    parser_version = "1.0"
    priority = 100

    signatures = (
        "axis bank ltd",
        "axis bank limited",
        "statement of account",
    )
    ifsc_prefixes = ("UTIB",)


registry.register(AxisBankParser())
