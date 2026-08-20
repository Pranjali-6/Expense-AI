"""Turn a statement PDF into something a human can review.

The builder's job is to make confirming a row cheap and correcting one
possible. So the proposal carries two things side by side: an image of each
page exactly as the bank drew it, and the rows the parser thinks are on that
page. The reviewer compares them.

The parser's output is a *proposal*, never an answer. A parser that reads
nothing — which is the interesting case, and the one synthetic fixtures never
produce — yields an empty row list and the reviewer types the statement in.
That is slow, and it is the price of ground truth that is actually ground
truth.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

#: Enough to read a statement on screen without making the page images so
#: large that the browser struggles with a seven-page document.
RENDER_DPI = 110


def _money(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


@dataclass(slots=True)
class Proposal:
    fixture: str
    source_name: str
    page_count: int
    #: base64 PNG data URIs, one per page, in page order.
    page_images: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    rows: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: What the parser did, shown in the UI so the reviewer knows how much to
    #: distrust the proposal before they start.
    parser_note: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "fixture": self.fixture,
            "source_name": self.source_name,
            "page_count": self.page_count,
            "page_images": self.page_images,
            "metadata": self.metadata,
            "rows": self.rows,
            "warnings": self.warnings,
            "parser_note": self.parser_note,
        }


def render_pages(data: bytes) -> list[str]:
    """Rasterise every page to a PNG data URI."""
    import fitz  # PyMuPDF

    images: list[str] = []
    with fitz.open(stream=data, filetype="pdf") as document:
        for page in document:
            pixmap = page.get_pixmap(dpi=RENDER_DPI)
            encoded = base64.b64encode(pixmap.tobytes("png")).decode("ascii")
            images.append(f"data:image/png;base64,{encoded}")
    return images


def build(pdf_path: Path) -> Proposal:
    """Parse a fixture and pair the result with page images."""
    from app.extraction.pipeline import parse_document

    from tools.corpus.fixtures import fixture_bytes

    data = fixture_bytes(pdf_path)
    outcome = parse_document(data)
    result = outcome.result
    metadata = result.metadata

    rows = [
        {
            # Every proposed row starts unconfirmed. Nothing reaches
            # expected.json on the parser's say-so alone.
            "confirmed": False,
            # Parsers number pages from 1; the review page indexes its image
            # list from 0. Converted here so the UI never has to know.
            "source_page": max(transaction.source_page - 1, 0),
            "txn_date": transaction.txn_date.isoformat(),
            "value_date": (
                transaction.value_date.isoformat() if transaction.value_date else None
            ),
            "description": transaction.description,
            "amount": str(transaction.amount),
            "direction": str(transaction.direction),
            "balance_after": _money(transaction.balance_after),
            "reference": transaction.reference,
            "merchant_normalized": transaction.merchant_normalized,
            "merchant_slug": transaction.merchant_slug,
            "payment_method": str(transaction.payment_method),
            "category_slug": transaction.category_slug,
            "subcategory_slug": transaction.subcategory_slug,
        }
        for transaction in result.transactions
    ]

    images = render_pages(data)

    note = (
        f"{metadata.bank_code} parser proposed {len(rows)} rows"
        f" ({result.unparsed_row_count} unparsed)."
    )
    if not rows:
        note += " It read nothing — every row has to be entered by hand."

    return Proposal(
        fixture=pdf_path.stem,
        source_name=pdf_path.name,
        page_count=len(images),
        page_images=images,
        metadata={
            "bank_code": metadata.bank_code,
            "bank_name": metadata.bank_name,
            "document_type": str(metadata.document_type),
            "account_last4": metadata.account_last4,
            "period_start": (
                metadata.period_start.isoformat() if metadata.period_start else None
            ),
            "period_end": (
                metadata.period_end.isoformat() if metadata.period_end else None
            ),
            "opening_balance": _money(metadata.opening_balance),
            "closing_balance": _money(metadata.closing_balance),
            "declared_transaction_count": metadata.declared_transaction_count,
            "total_amount_due": _money(metadata.total_amount_due),
            "minimum_amount_due": _money(metadata.minimum_amount_due),
            "payment_due_date": (
                metadata.payment_due_date.isoformat()
                if metadata.payment_due_date
                else None
            ),
            "credit_limit": _money(metadata.credit_limit),
        },
        rows=rows,
        warnings=list(result.warnings),
        parser_note=note,
    )
