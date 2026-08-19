"""Yes Bank statements — the tuned generic parser with an identity.

Nothing about this layout deviates from the shared tabular reader, so the parser
declares what the bank calls itself and inherits the rest. That is the intended
shape: a bank earns a dedicated parser by having a quirk worth code, not by
existing.
"""

from __future__ import annotations

from parsers.banks.generic import TabularBankParser
from parsers.registry import registry


class YESBankParser(TabularBankParser):
    bank_code = "YES"
    bank_name = "Yes Bank"
    parser_name = "yesbank_bank"
    parser_version = "1.0"
    priority = 50

    signatures = ("yes bank",)
    ifsc_prefixes = ("YESB",)


registry.register(YESBankParser())
