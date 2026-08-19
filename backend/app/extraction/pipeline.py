"""Read a statement PDF into the canonical schema.

    bytes → text layer → (OCR if needed) → tables → classify → dispatch → parse

This is the only place that knows the order of those steps, and it is
deliberately linear: every stage's output is inspectable, every stage can fail
independently, and no stage can be skipped by a parser deciding it knows better.

Nothing here calls a language model.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.extraction import ocr, table_extract, text_extract
from app.extraction.classifier import classify_document
from app.models.enums import DocumentType, ExtractionMethod

from parsers.canonical import ParseResult
from parsers.document import ExtractedDocument
from parsers.registry import Dispatch, load_parsers

logger = get_logger(__name__)


@dataclass(slots=True)
class ExtractionOutcome:
    document: ExtractedDocument
    document_type: DocumentType
    classification_confidence: float
    dispatch: Dispatch
    result: ParseResult
    warnings: list[str] = field(default_factory=list)

    @property
    def method(self) -> ExtractionMethod:
        return self.document.method

    @property
    def ocr_page_count(self) -> int:
        return self.document.ocr_page_count


def read_document(data: bytes, *, allow_ocr: bool = True) -> ExtractedDocument:
    """Produce an ``ExtractedDocument`` from PDF bytes."""
    document = text_extract.extract_text_layer(data)

    if allow_ocr and ocr.needs_ocr(document):
        document = ocr.apply_ocr(data, document)

    try:
        document = table_extract.attach_tables(data, document)
    except Exception as exc:
        # Table extraction is an optimisation: every parser can fall back to
        # reading text lines. Losing it degrades accuracy; it must not lose the
        # statement.
        document.warnings.append("table_extraction_failed")
        logger.warning(
            "table_extraction_failed", stage="extract", error_code=type(exc).__name__
        )

    return document


def parse_document(data: bytes, *, allow_ocr: bool = True) -> ExtractionOutcome:
    """Full read: extraction, classification, parser dispatch and parse."""
    return parse_extracted(read_document(data, allow_ocr=allow_ocr))


def parse_extracted(document: ExtractedDocument) -> ExtractionOutcome:
    """Classify, dispatch and parse an already-read document.

    Split out from :func:`parse_document` so the worker can emit a progress
    stage between reading and parsing. Two entry points, one implementation —
    a second copy of the dispatch logic is how the pipeline and the accuracy
    harness end up measuring different things.
    """
    registry = load_parsers()

    document_type, confidence = classify_document(document.header_text(3))
    dispatch = registry.resolve(document, document_type=document_type)

    result = dispatch.parser.parse(document)

    # The classifier has the header; the parser has the whole document. When the
    # parser is sure and the classifier was not, the parser wins.
    if document_type == DocumentType.UNKNOWN and result.metadata.document_type != DocumentType.UNKNOWN:
        document_type = result.metadata.document_type
    result.metadata.document_type = document_type

    warnings = [*document.warnings, *result.warnings]
    if dispatch.is_fallback:
        warnings.append("generic_parser_used")

    logger.info(
        "statement_parsed",
        stage="parse",
        bank_code=result.metadata.bank_code or "unknown",
        count=len(result.transactions),
        status="ok" if result.transactions else "empty",
    )

    return ExtractionOutcome(
        document=document,
        document_type=document_type,
        classification_confidence=confidence,
        dispatch=dispatch,
        result=result,
        warnings=warnings,
    )
