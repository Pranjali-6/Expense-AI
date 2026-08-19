"""Text-layer extraction.

PyMuPDF is the primary reader: it is fast, it preserves reading order well, and
it reports per-page character counts, which is what decides whether OCR is
needed at all.

An LLM is never involved. That is not a performance choice — a language model
asked to read a statement will produce a plausible transaction list whether or
not it could actually see the numbers, and a plausible wrong number in a ledger
is worse than a loud failure.
"""

from __future__ import annotations

from app.core.config import settings
from app.models.enums import ExtractionMethod

from parsers.document import ExtractedDocument, ExtractedPage


def extract_text_layer(data: bytes) -> ExtractedDocument:
    """Read every page's text layer. Pages below the density floor are flagged."""
    import fitz

    document = ExtractedDocument()
    with fitz.open(stream=data, filetype="pdf") as pdf:
        for index, page in enumerate(pdf, start=1):
            content = page.get_text("text") or ""
            needs_ocr = len(content.strip()) < settings.OCR_MIN_CHARS_PER_PAGE
            document.pages.append(
                ExtractedPage(
                    page_number=index,
                    text=content,
                    method=ExtractionMethod.OCR if needs_ocr else ExtractionMethod.TEXT_LAYER,
                )
            )
    return document


def page_char_counts(data: bytes) -> list[int]:
    import fitz

    with fitz.open(stream=data, filetype="pdf") as pdf:
        return [len((page.get_text("text") or "").strip()) for page in pdf]
