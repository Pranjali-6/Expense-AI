"""The extraction layer: text, tables, OCR routing and classification.

The layer's job is to hand parsers a faithful view of the document. Its failure
mode is not an exception — it is a *plausible* view that is subtly wrong, which
a parser will read confidently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.models.enums import DocumentType, ExtractionMethod

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "statements"

pytestmark = pytest.mark.skipif(
    not FIXTURES.exists() or not any(FIXTURES.glob("*.pdf")),
    reason="run `make gen-fixtures` first",
)


def _bytes(name: str) -> bytes:
    return (FIXTURES / f"{name}.pdf").read_bytes()


class TestTextLayerReading:
    def test_a_text_layer_pdf_is_read_without_ocr(self):
        from app.extraction.text_extract import extract_text_layer

        document = extract_text_layer(_bytes("hdfc-2024-03"))

        assert len(document.pages) == 3
        assert document.method == ExtractionMethod.TEXT_LAYER
        assert document.ocr_page_count == 0
        assert all(page.char_count > 500 for page in document.pages)

    def test_a_scanned_pdf_is_routed_to_ocr(self):
        """The density check that decides this was once measured in characters
        per square point against a threshold that needed ~10,000 characters on
        an A4 page, so text-layer statements were sent to OCR."""
        from app.extraction.text_extract import extract_text_layer

        document = extract_text_layer(_bytes("sbi-2024-04-scanned"))

        assert document.method == ExtractionMethod.OCR
        assert document.ocr_page_count == len(document.pages)


class TestTableExtraction:
    def test_ruled_tables_are_recovered_with_their_columns(self):
        from app.extraction.table_extract import attach_tables
        from app.extraction.text_extract import extract_text_layer

        data = _bytes("hdfc-2024-03")
        document = attach_tables(data, extract_text_layer(data))

        tables = document.all_tables()
        assert tables, "no tables found in a fully ruled statement"
        # HDFC prints seven columns; anything narrower means columns merged.
        assert max(table.width for table in tables) >= 7

    def test_extraction_survives_a_document_with_no_tables(self):
        """Table extraction is an optimisation. Losing it must degrade
        accuracy, not lose the statement."""
        from app.extraction.pipeline import read_document

        document = read_document(_bytes("sbi-2024-04-scanned"))
        assert document.pages
        assert all(page.tables == [] for page in document.pages)


class TestClassification:
    @pytest.mark.parametrize(
        ("fixture", "expected"),
        [
            ("hdfc-2024-03", DocumentType.BANK_STATEMENT),
            ("icici-2024-03", DocumentType.BANK_STATEMENT),
            ("hdfc-card-2024-03", DocumentType.CREDIT_CARD_STATEMENT),
            ("icici-card-2024-03", DocumentType.CREDIT_CARD_STATEMENT),
        ],
    )
    def test_bank_and_card_statements_are_told_apart(self, fixture, expected):
        from app.extraction.classifier import classify_document
        from app.extraction.text_extract import extract_text_layer

        document = extract_text_layer(_bytes(fixture))
        kind, confidence = classify_document(document.header_text(3))

        assert kind == expected
        assert confidence > 0.5

    def test_an_empty_document_is_unknown_rather_than_guessed(self):
        from app.extraction.classifier import classify_document

        assert classify_document("")[0] == DocumentType.UNKNOWN


class TestPipelineOutcome:
    def test_the_outcome_reports_how_the_document_was_read(self):
        from app.extraction.pipeline import parse_document

        outcome = parse_document(_bytes("hdfc-2024-03"))

        assert outcome.method == ExtractionMethod.TEXT_LAYER
        assert outcome.dispatch.parser.bank_code == "HDFC"
        assert outcome.dispatch.is_fallback is False
        assert outcome.result.transactions

    def test_a_generic_read_is_declared_not_hidden(self):
        from app.extraction.pipeline import parse_document

        outcome = parse_document(_bytes("generic-2024-03"))

        assert outcome.dispatch.is_fallback is True
        assert "generic_parser_used" in outcome.warnings

    def test_every_transaction_is_traceable_to_its_page(self):
        """Statement Health says "the balance diverges at row 47 on page 3".
        That is only possible if provenance survives parsing."""
        from app.extraction.pipeline import parse_document

        outcome = parse_document(_bytes("axis-2024-04-multipage"))

        for transaction in outcome.result.transactions:
            assert transaction.source_page is not None
            assert transaction.source_row is not None


class TestNoModelIsInvolved:
    def test_the_extraction_stack_imports_no_ai_provider(self):
        """The governing invariant: an LLM is never the extraction engine.

        Asked to read a statement, a language model will produce a plausible
        transaction list whether or not it could see the numbers — and a
        plausible wrong number in a ledger is worse than a loud failure.
        """
        import app.extraction.classifier
        import app.extraction.ocr
        import app.extraction.pipeline
        import app.extraction.table_extract
        import app.extraction.text_extract
        import parsers.banks.generic
        import parsers.base

        modules = (
            app.extraction.pipeline, app.extraction.text_extract,
            app.extraction.table_extract, app.extraction.ocr,
            app.extraction.classifier, parsers.base, parsers.banks.generic,
        )
        for module in modules:
            source = Path(module.__file__).read_text()
            for forbidden in ("google.genai", "openai", "anthropic", "app.ai."):
                assert forbidden not in source, f"{module.__name__} references {forbidden}"
