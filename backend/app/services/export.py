"""Taking your data with you.

Three formats, one rule: **an export is the ledger as the user sees it, not the
ledger as the system stores it.** Effective values, not the frozen originals;
category names, not ids; account masks, not numbers. Someone opening a CSV in a
spreadsheet is checking their own money against a bank statement, and an id
column helps nobody do that.

Two decisions worth stating.

**Exports are streamed, never stored.** There is a MinIO bucket for them and the
obvious design is to write the file, hand back a presigned URL and let the
browser fetch it. That design puts a plaintext copy of an entire financial
history at rest, encrypted or not, for as long as the retention sweep takes to
notice. Streaming it once down an already-authenticated connection means the
file never exists anywhere but the user's disk.

**Money is written as an exact decimal string, not a formatted one.** ``1234.56``
rather than ``₹1,234.56``: the second is prettier and the first is the one a
spreadsheet will add up correctly. The PDF is the exception — it is for reading,
so it is formatted with Indian grouping.

Rows are capped. An unbounded export is an unbounded query, an unbounded
response and an unbounded memory footprint, and the cap is high enough that a
decade of statements fits inside it.
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from collections.abc import Iterator
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationFailedError
from app.observability import metrics
from app.services import transactions as txn_service

#: Roughly a decade of heavy use. Beyond this the honest answer is a date range,
#: not a bigger buffer.
MAX_ROWS = 50_000

FORMATS = ("csv", "json", "pdf")

#: The exported shape, declared once. A positive projection: a column added to
#: the ledger does not silently start appearing in everyone's exports.
COLUMNS: tuple[tuple[str, str], ...] = (
    ("txn_date", "Date"),
    ("description", "Description"),
    ("merchant", "Merchant"),
    ("category_name", "Category"),
    ("subcategory_name", "Subcategory"),
    ("direction", "Direction"),
    ("amount", "Amount"),
    ("payment_method", "Method"),
    ("movement_type", "Type"),
    ("is_expense", "Counted as spending"),
    ("bank_name", "Bank"),
    ("account_last4", "Account"),
    ("review_status", "Status"),
    ("is_verified", "Verified by you"),
    ("statement_trust_status", "Statement reconciled"),
)


def _cell(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return str(value)


async def gather(
    session: AsyncSession,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    category_slug: str | None = None,
    account_id: uuid.UUID | None = None,
    merchant: str | None = None,
    search: str | None = None,
    direction: str | None = None,
    review_status: str | None = None,
    is_expense: bool | None = None,
    min_amount: Decimal | None = None,
    max_amount: Decimal | None = None,
) -> list[dict[str, Any]]:
    """The rows to export, oldest first.

    Ascending by date, unlike the ledger screen. A screen shows the newest
    first because that is what you came to look at; a file is read top to
    bottom like a bank statement.
    """
    rows, total = await txn_service.list_transactions(
        session,
        date_from=date_from,
        date_to=date_to,
        category_slug=category_slug,
        account_id=account_id,
        merchant=merchant,
        search=search,
        direction=direction,
        review_status=review_status,
        is_expense=is_expense,
        min_amount=min_amount,
        max_amount=max_amount,
        limit=MAX_ROWS,
        offset=0,
    )
    if total > MAX_ROWS:
        raise ValidationFailedError(
            f"That selection has {total} transactions, more than the "
            f"{MAX_ROWS} an export can hold. Narrow the date range.",
            error_code="export_too_large",
        )
    rows.sort(key=lambda item: (item["txn_date"], item["created_at"]))
    return rows


def filename(fmt: str, *, when: datetime | None = None) -> str:
    stamp = (when or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M")
    return f"expense-ai-transactions-{stamp}.{fmt}"


# --------------------------------------------------------------------------- #
# Renderers
# --------------------------------------------------------------------------- #

def to_csv(rows: list[dict[str, Any]]) -> Iterator[bytes]:
    """Stream CSV, a chunk of rows at a time.

    Written with ``\\r\\n`` and a UTF-8 BOM because the most likely destination
    is Excel on Windows, which reads a BOM-less UTF-8 file as Latin-1 and turns
    every ₹ into mojibake. The BOM costs three bytes and is invisible to
    everything else.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")

    writer.writerow([label for _, label in COLUMNS])
    yield b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8")
    buffer.seek(0)
    buffer.truncate(0)

    for index, row in enumerate(rows, start=1):
        writer.writerow([_cell(row, key) for key, _ in COLUMNS])
        if index % 500 == 0:
            yield buffer.getvalue().encode("utf-8")
            buffer.seek(0)
            buffer.truncate(0)

    remainder = buffer.getvalue()
    if remainder:
        yield remainder.encode("utf-8")


def to_json(rows: list[dict[str, Any]]) -> Iterator[bytes]:
    """Stream JSON with money as strings.

    Numbers in JSON are IEEE 754 doubles the moment anything parses them, and
    ``0.1 + 0.2`` is the reason this codebase never lets a float near a rupee.
    An importer that wants arithmetic can parse the string into its own decimal
    type; one that gets a float has already lost paise it cannot recover.
    """
    yield b'{\n  "exported_at": "%s",\n  "transaction_count": %d,\n  "transactions": [\n' % (
        datetime.now(timezone.utc).isoformat().encode("ascii"),
        len(rows),
    )
    for index, row in enumerate(rows):
        payload = {label.lower().replace(" ", "_"): _cell(row, key) for key, label in COLUMNS}
        suffix = b",\n" if index < len(rows) - 1 else b"\n"
        yield b"    " + json.dumps(payload, ensure_ascii=False).encode("utf-8") + suffix
    yield b"  ]\n}\n"


def _inr(value: Decimal | None) -> str:
    """Indian digit grouping, for the format meant to be read rather than parsed."""
    if value is None:
        return ""
    whole, _, frac = f"{abs(value):.2f}".partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts: list[str] = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join([*parts, tail])
    return f"{whole}.{frac}"


def to_pdf(rows: list[dict[str, Any]], *, title: str = "Transactions") -> bytes:
    """A readable statement, not a data interchange format.

    Deliberately narrow: date, description, category, direction, amount. A PDF
    that tries to carry every column becomes six-point type nobody can read, and
    anyone who wants every column wants the CSV.
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=14 * mm,
        rightMargin=14 * mm,
        topMargin=14 * mm,
        bottomMargin=16 * mm,
        title=title,
        author="Expense AI",
    )

    styles = getSampleStyleSheet()
    cell = ParagraphStyle("cell", parent=styles["BodyText"], fontSize=7.5, leading=9.5)
    head = ParagraphStyle("head", parent=cell, fontName="Helvetica-Bold", textColor=colors.white)

    debits = sum(
        (row["amount"] for row in rows if row["direction"] == "debit"), Decimal("0.00")
    )
    credits = sum(
        (row["amount"] for row in rows if row["direction"] == "credit"), Decimal("0.00")
    )

    story: list[Any] = [
        Paragraph(f"<b>{title}</b>", styles["Title"]),
        Paragraph(
            f"{len(rows)} transactions · out ₹{_inr(debits)} · in ₹{_inr(credits)}"
            f" · generated {datetime.now(timezone.utc).strftime('%d %b %Y')}",
            styles["Normal"],
        ),
        Spacer(1, 6 * mm),
    ]

    data: list[list[Any]] = [
        [Paragraph(text, head) for text in ("Date", "Description", "Category", "Dr/Cr", "Amount (₹)")]
    ]
    for row in rows:
        data.append(
            [
                Paragraph(_cell(row, "txn_date"), cell),
                Paragraph(
                    (row.get("merchant") or row.get("description") or "")[:70], cell
                ),
                Paragraph(row.get("category_name") or "—", cell),
                Paragraph("Dr" if row["direction"] == "debit" else "Cr", cell),
                Paragraph(_inr(row["amount"]), cell),
            ]
        )

    table = Table(
        data,
        colWidths=[20 * mm, 78 * mm, 30 * mm, 12 * mm, 32 * mm],
        repeatRows=1,
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0B1220")),
                ("ALIGN", (3, 0), (-1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#E2E8F0")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    story.append(table)

    document.build(story)
    return buffer.getvalue()


def render(fmt: str, rows: list[dict[str, Any]]) -> tuple[Iterator[bytes], str]:
    """Body and media type for one format."""
    if fmt not in FORMATS:
        raise ValidationFailedError(f"{fmt!r} is not an export format.")

    metrics.exports_total.labels(format=fmt).inc()
    metrics.export_rows_total.inc(len(rows))

    if fmt == "csv":
        return to_csv(rows), "text/csv; charset=utf-8"
    if fmt == "json":
        return to_json(rows), "application/json"
    return iter([to_pdf(rows)]), "application/pdf"
