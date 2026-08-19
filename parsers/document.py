"""What extraction hands to a parser.

The extraction layer produces this; parsers consume it and nothing else. That
boundary is what lets a parser be tested against a hand-built document with no
PDF anywhere in sight, and what lets the extraction stack change (pdfplumber to
Camelot to OCR) without touching a single bank parser.

Both views of a page are carried deliberately. Tables are the better source when
the PDF has ruled cells or clean column geometry; the raw text lines are the
better source when it does not, which on Indian statements is often. A parser
picks, and the ones that can do both fall back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

from app.models.enums import ExtractionMethod


@dataclass(slots=True)
class Table:
    """A table as extracted, with cells exactly as they were read.

    Cells are never trimmed of their content here — only of surrounding
    whitespace — because deciding that a cell is empty is a parser's judgement,
    not an extractor's.
    """

    rows: list[list[str]]
    source: str = "pdfplumber"
    page_number: int = 0
    # Column x-positions when the extractor knows them. A parser can use these
    # to tell a Withdrawal column from a Deposit column when the header text is
    # ambiguous or missing on continuation pages.
    column_positions: list[float] = field(default_factory=list)

    @property
    def width(self) -> int:
        return max((len(row) for row in self.rows), default=0)

    def cell(self, row: int, column: int) -> str:
        try:
            value = self.rows[row][column]
        except IndexError:
            return ""
        return (value or "").strip()


@dataclass(slots=True)
class ExtractedPage:
    page_number: int
    text: str = ""
    tables: list[Table] = field(default_factory=list)
    method: ExtractionMethod = ExtractionMethod.TEXT_LAYER
    ocr_confidence: float | None = None

    @property
    def char_count(self) -> int:
        return len(self.text.strip())

    @property
    def lines(self) -> list[str]:
        """Non-empty text lines, in reading order."""
        return [line for line in (raw.rstrip() for raw in self.text.splitlines()) if line.strip()]


@dataclass(slots=True)
class ExtractedDocument:
    pages: list[ExtractedPage] = field(default_factory=list)
    # Structural warnings from extraction, as codes. Never page content.
    warnings: list[str] = field(default_factory=list)

    def __iter__(self) -> Iterator[ExtractedPage]:
        return iter(self.pages)

    def __len__(self) -> int:
        return len(self.pages)

    @property
    def full_text(self) -> str:
        return "\n".join(page.text for page in self.pages)

    @property
    def method(self) -> ExtractionMethod:
        """How the document as a whole was read.

        ``HYBRID`` is reported honestly when some pages had a text layer and
        others were scanned — a common shape for a statement with a scanned
        cheque image appended, and one where the OCR pages deserve lower
        extraction confidence than the rest.
        """
        methods = {page.method for page in self.pages}
        if methods == {ExtractionMethod.TEXT_LAYER}:
            return ExtractionMethod.TEXT_LAYER
        if methods == {ExtractionMethod.OCR}:
            return ExtractionMethod.OCR
        return ExtractionMethod.HYBRID

    @property
    def ocr_page_count(self) -> int:
        return sum(1 for page in self.pages if page.method == ExtractionMethod.OCR)

    def header_text(self, pages: int = 2) -> str:
        """Text from the opening pages, where bank identity and metadata live."""
        return "\n".join(page.text for page in self.pages[:pages])

    def masthead(self, max_lines: int = 40) -> str:
        """Text above the first transaction row on page one.

        Bank *detection* must read this and not ``header_text``. A statement's
        narrations routinely name other banks — a UPI payment carries the
        payee's bank, an IMPS transfer carries the beneficiary's — so scanning
        the transaction table for issuer signatures lets a counterparty
        out-score the actual issuer. That is not hypothetical: it is exactly how
        an Axis statement full of ``UPI/P2M/…/YES BANK`` narrations was
        dispatched to the Yes Bank parser.

        The boundary is found structurally rather than by line count: the first
        line that *starts* with a date is the first transaction row. Header
        lines mention dates ("Statement From 01/03/2024") but do not begin with
        one.
        """
        from parsers.normalizers.dates import leading_date

        if not self.pages:
            return ""

        collected: list[str] = []
        for line in self.pages[0].lines[:max_lines]:
            if leading_date(line) is not None:
                break
            collected.append(line)

        # A layout that opens straight into the table leaves nothing to match
        # on; fall back to a bounded slice rather than to no detection at all.
        if len(collected) < 3:
            collected = self.pages[0].lines[:20]

        return "\n".join(collected)

    def all_lines(self) -> list[tuple[int, str]]:
        """Every line in the document paired with its page number."""
        return [
            (page.page_number, line)
            for page in self.pages
            for line in page.lines
        ]

    def all_tables(self) -> list[Table]:
        return [table for page in self.pages for table in page.tables]
