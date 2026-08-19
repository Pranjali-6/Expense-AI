"""IndusInd Bank statements — the tuned generic parser with an identity.

Nothing about this layout deviates from the shared tabular reader, so the parser
declares what the bank calls itself and inherits the rest. That is the intended
shape: a bank earns a dedicated parser by having a quirk worth code, not by
existing.
"""

from __future__ import annotations

from parsers.banks.generic import TabularBankParser
from parsers.registry import registry


class INDUSINDBankParser(TabularBankParser):
    bank_code = "INDUSIND"
    bank_name = "IndusInd Bank"
    parser_name = "indusind_bank"
    parser_version = "1.0"
    priority = 50

    signatures = ("indusind bank",)
    ifsc_prefixes = ("INDB",)


registry.register(INDUSINDBankParser())
