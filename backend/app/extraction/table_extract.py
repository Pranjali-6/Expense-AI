"""Table extraction, deterministic and layered.

Three strategies, tried in order of how much they assume:

1. **pdfplumber, line-based.** Statements with ruled cells — most net-banking
   exports — give exact cell boundaries. When it works it is exact.
2. **pdfplumber, whitespace-based.** No ruling lines, columns inferred from text
   alignment. Right most of the time, and wrong in a way that shows up as
   merged columns rather than invented numbers.
3. **Camelot lattice.** Slower and needs Ghostscript, but it recovers grids
   pdfplumber's line detection misses on low-contrast or hairline rules.

Each layer's output is checked for plausibility before it is accepted, because
a table extractor that returns *something* for every page is more dangerous than
one that returns nothing: a two-column mess that parses as transactions
produces confident garbage, while an empty result falls through to the text-line
parser, which for many Indian statements is the better reader anyway.
"""

from __future__ import annotations

import io

from parsers.document import ExtractedDocument, Table

# A table with fewer columns than this cannot hold date + description + amount,
# so it is page furniture (an address block, a summary box), not transactions.
_MIN_COLUMNS = 3
_MIN_ROWS = 2

_LINE_SETTINGS = {
    "vertical_strategy": "lines",
    "horizontal_strategy": "lines",
    "snap_tolerance": 3,
    "join_tolerance": 3,
    "intersection_tolerance": 3,
}

_TEXT_SETTINGS = {
    "vertical_strategy": "text",
    "horizontal_strategy": "text",
    "text_tolerance": 2,
    "intersection_tolerance": 5,
}


def _clean(rows: list[list[str | None]]) -> list[list[str]]:
    return [[(cell or "").strip() for cell in row] for row in rows]


def _plausible(rows: list[list[str]]) -> bool:
    if len(rows) < _MIN_ROWS:
        return False
    width = max((len(row) for row in rows), default=0)
    if width < _MIN_COLUMNS:
        return False
    # A table that is almost entirely empty cells is a layout artefact.
    filled = sum(1 for row in rows for cell in row if cell)
    return filled >= len(rows)


def attach_tables(data: bytes, document: ExtractedDocument) -> ExtractedDocument:
    """Extract tables page by page and attach them to an already-read document."""
    import pdfplumber

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in document.pages:
            if page.page_number > len(pdf.pages):
                continue
            plumber_page = pdf.pages[page.page_number - 1]

            tables = _extract_page(plumber_page, page.page_number)
            if not tables:
                tables = _camelot_page(data, page.page_number)
            page.tables = tables

    return document


def _extract_page(plumber_page, page_number: int) -> list[Table]:
    found: list[Table] = []

    for settings, source in ((_LINE_SETTINGS, "pdfplumber_lines"),
                             (_TEXT_SETTINGS, "pdfplumber_text")):
        try:
            raw_tables = plumber_page.extract_tables(settings)
        except Exception:
            # A malformed page should cost this one strategy, not the document.
            continue

        candidates = []
        for raw in raw_tables or []:
            rows = _clean(raw)
            if _plausible(rows):
                candidates.append(
                    Table(rows=rows, source=source, page_number=page_number)
                )
        if candidates:
            found = candidates
            break

    return found


def _camelot_page(data: bytes, page_number: int) -> list[Table]:
    """Last-resort grid recovery. Camelot needs a file, so this writes one."""
    import tempfile
    from pathlib import Path

    try:
        import camelot
    except Exception:
        return []

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "page.pdf"
        path.write_bytes(data)
        try:
            tables = camelot.read_pdf(
                str(path), pages=str(page_number), flavor="lattice", suppress_stdout=True
            )
        except Exception:
            return []

        found: list[Table] = []
        for table in tables:
            rows = _clean(table.df.values.tolist())
            if _plausible(rows):
                found.append(Table(rows=rows, source="camelot_lattice",
                                   page_number=page_number))
        return found
