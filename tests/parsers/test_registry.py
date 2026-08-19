"""Parser dispatch.

Choosing the wrong parser is worse than having no parser. A bank-statement
parser aimed at a credit-card statement reads the summary block as transactions
and produces a confident, wrong ledger; refusing to read would at least be
obvious.
"""

from __future__ import annotations

import pytest

from app.models.enums import DocumentType

from parsers.document import ExtractedDocument, ExtractedPage
from parsers.registry import load_parsers


@pytest.fixture(scope="module")
def registry():
    return load_parsers()


def _document(masthead: str, rows: str = "") -> ExtractedDocument:
    text = masthead if not rows else f"{masthead}\n{rows}"
    return ExtractedDocument(pages=[ExtractedPage(page_number=1, text=text)])


HDFC_MASTHEAD = """HDFC BANK LIMITED
Statement of Account
Ananya Deshpande
Account Number: 27780550406458
IFSC: HDFC0269204    Branch: Bandra West
Statement From 01/03/2024 to 31/03/2024
Opening Balance: 1,84,320.55
Closing Balance: 83,127.62"""

AXIS_MASTHEAD = """AXIS BANK LTD
Statement of Account
Rohan Iyer
Account Number: 91720045123456
IFSC: UTIB0000123    Branch: Koramangala
Statement of Account for the period 01/03/2024 to 31/03/2024
Opening Balance: 58,990.75
Closing Balance: 21,968.18"""


class TestIssuerDetection:
    def test_hdfc_is_recognised(self, registry):
        dispatch = registry.resolve(
            _document(HDFC_MASTHEAD), document_type=DocumentType.BANK_STATEMENT
        )
        assert dispatch.parser.bank_code == "HDFC"
        assert dispatch.is_fallback is False

    def test_axis_is_recognised(self, registry):
        dispatch = registry.resolve(
            _document(AXIS_MASTHEAD), document_type=DocumentType.BANK_STATEMENT
        )
        assert dispatch.parser.bank_code == "AXIS"


class TestACounterpartyBankCannotHijackDetection:
    """Regression: an Axis statement was dispatched to the Yes Bank parser.

    Detection read the whole first two pages, transaction table included. Axis
    formats UPI narrations as ``UPI/P2M/<ref>/<payee>/YES BANK`` — the payee's
    bank, printed on every row — which out-scored Axis's own masthead. Detection
    is now bounded to the text above the first transaction row.
    """

    AXIS_ROWS = "\n".join(
        f"0{day}-03-2024  UPI/P2M/41234567890{day}/SWIGGY/YES BANK   441.00   2,34,969.74"
        for day in range(1, 10)
    )

    def test_axis_wins_despite_yes_bank_on_every_row(self, registry):
        dispatch = registry.resolve(
            _document(AXIS_MASTHEAD, self.AXIS_ROWS),
            document_type=DocumentType.BANK_STATEMENT,
        )
        assert dispatch.parser.bank_code == "AXIS"

    def test_the_masthead_stops_at_the_first_transaction_row(self):
        document = _document(AXIS_MASTHEAD, self.AXIS_ROWS)
        masthead = document.masthead()

        assert "AXIS BANK LTD" in masthead
        assert "YES BANK" not in masthead

    def test_a_header_line_mentioning_a_date_is_still_masthead(self):
        """"Statement of Account for the period 01/03/2024 …" must not end it."""
        assert "period 01/03/2024" in _document(AXIS_MASTHEAD).masthead()

    def test_an_ifsc_in_a_beneficiary_narration_does_not_count(self, registry):
        """A payment *to* an HDFC account is not an HDFC statement."""
        rows = "01-03-2024  NEFT/123/SOMEONE/HDFC0001234   5,000.00   50,000.00"
        dispatch = registry.resolve(
            _document(AXIS_MASTHEAD, rows), document_type=DocumentType.BANK_STATEMENT
        )
        assert dispatch.parser.bank_code == "AXIS"


class TestDocumentTypeIsAHardFilter:
    CARD_MASTHEAD = """HDFC BANK LIMITED
Credit Card Statement
Ananya Deshpande
Card Number: 4719XXXXXXXX5164
Statement Period: 01/03/2024 - 31/03/2024
Payment Due Date: 18/04/2024
Total Amount Due: 38,279.12
Minimum Amount Due: 1,913.96"""

    def test_a_card_statement_goes_to_a_card_parser(self, registry):
        dispatch = registry.resolve(
            _document(self.CARD_MASTHEAD),
            document_type=DocumentType.CREDIT_CARD_STATEMENT,
        )
        assert DocumentType.CREDIT_CARD_STATEMENT in dispatch.parser.document_types
        assert dispatch.parser.bank_code == "HDFC"

    def test_a_bank_parser_is_never_offered_a_card_statement(self, registry):
        dispatch = registry.resolve(
            _document(self.CARD_MASTHEAD),
            document_type=DocumentType.CREDIT_CARD_STATEMENT,
        )
        assert DocumentType.BANK_STATEMENT not in dispatch.parser.document_types


class TestFallback:
    def test_an_unknown_bank_falls_back_rather_than_failing(self, registry):
        """Reading an unrecognised bank generically and reconciling to ₹0.00 is
        a good outcome. Refusing to read it is not."""
        unknown = """SAHYADRI COOPERATIVE BANK LTD
Statement of Account
Account Number: 40012345678901
Opening Balance: 88,120.65"""
        dispatch = registry.resolve(
            _document(unknown), document_type=DocumentType.BANK_STATEMENT
        )
        assert dispatch.is_fallback is True
        assert dispatch.parser.parser_name == "generic_bank"
        assert dispatch.confidence == 0.0

    def test_every_candidates_score_is_reported(self, registry):
        dispatch = registry.resolve(
            _document(HDFC_MASTHEAD), document_type=DocumentType.BANK_STATEMENT
        )
        names = {name for name, _ in dispatch.scores}
        assert "hdfc_bank" in names and "icici_bank" in names


class TestRegistryCompleteness:
    def test_all_ten_parsers_are_registered(self, registry):
        names = {parser.parser_name for parser in registry.all()}
        assert names == {
            "hdfc_bank", "icici_bank", "sbi_bank", "axis_bank",
            "kotak_bank", "idfc_bank", "indusind_bank", "yesbank_bank",
            "generic_bank", "generic_card", "hdfc_card", "icici_card",
        }

    def test_a_generic_parser_exists_for_both_document_types(self, registry):
        generics = [p for p in registry.all() if p.priority == 0]
        covered = {kind for parser in generics for kind in parser.document_types}
        assert covered == {DocumentType.BANK_STATEMENT, DocumentType.CREDIT_CARD_STATEMENT}
