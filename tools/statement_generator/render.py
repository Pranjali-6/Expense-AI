"""Renders a generated ledger into a bank-realistic PDF.

Pagination is done by hand rather than left to Platypus. Real statements carry
a brought-forward balance line at the top of every continuation page and a
carried-forward line at the foot, and a parser has to *not* read those as
transactions — which makes them one of the more valuable things a fixture can
contain. Automatic table splitting would not produce them.

The output is a genuine text-layer PDF. The scanned variant rasterises that same
document so the OCR path is exercised against a page whose correct answer is
already known exactly.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.platypus import Paragraph, Table, TableStyle

from app.models.enums import Direction

from tools.statement_generator.ledger import GeneratedStatement, LedgerEntry
from tools.statement_generator.spec import (
    SIGNED_CREDIT_ONLY,
    SIGNED_MINUS,
    SIGNED_SUFFIX,
    SPLIT_COLUMNS,
)

PAGE_WIDTH, PAGE_HEIGHT = A4
MARGIN = 12 * mm

INK = colors.HexColor("#111827")
MUTED = colors.HexColor("#4B5563")
RULE = colors.HexColor("#9CA3AF")
BAND = colors.HexColor("#EFF2F6")


def format_inr(value: Decimal) -> str:
    """Indian digit grouping: 1,23,456.78 — not 123,456.78.

    Built by hand from the string form. Python's ``format`` and ``locale`` both
    do Western three-digit grouping, and money is never handled here as anything
    but an exact Decimal, so there is no float round-trip to introduce a
    rounding error on the way to the page.
    """
    negative = value < 0
    whole, _, frac = f"{abs(value):.2f}".partition(".")

    if len(whole) <= 3:
        grouped = whole
    else:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        grouped = ",".join([*parts, tail])

    return f"{'-' if negative else ''}{grouped}.{frac}"


def _amount_cell(entry: LedgerEntry, role: str, style: str) -> str:
    if style == SPLIT_COLUMNS:
        if role == "debit":
            return format_inr(entry.amount) if entry.direction == Direction.DEBIT else ""
        if role == "credit":
            return format_inr(entry.amount) if entry.direction == Direction.CREDIT else ""
    if role == "amount":
        text = format_inr(entry.amount)
        if style == SIGNED_SUFFIX:
            return f"{text} {'Dr' if entry.direction == Direction.DEBIT else 'Cr'}"
        if style == SIGNED_CREDIT_ONLY:
            return f"{text} CR" if entry.direction == Direction.CREDIT else text
        if style == SIGNED_MINUS:
            return f"-{text}" if entry.direction == Direction.DEBIT else text
        return text
    return ""


class _StatementCanvas(pdfcanvas.Canvas):
    """Adds `Page N of M` once the total page count is known."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._pages: list[dict] = []

    def showPage(self) -> None:  # noqa: N802 - reportlab's API
        self._pages.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        total = len(self._pages)
        for state in self._pages:
            self.__dict__.update(state)
            self.setFont("Helvetica", 6.5)
            self.setFillColor(MUTED)
            self.drawRightString(
                PAGE_WIDTH - MARGIN, 8 * mm, f"Page {self._pageNumber} of {total}"
            )
            super().showPage()
        super().save()


def _cell_style(size: float, *, bold: bool = False, align: int = 0) -> ParagraphStyle:
    return ParagraphStyle(
        name=f"cell{size}{bold}{align}",
        fontName="Helvetica-Bold" if bold else "Helvetica",
        fontSize=size,
        leading=size + 1.6,
        textColor=INK,
        alignment=align,
    )


def _column_widths(statement: GeneratedStatement, available: float) -> list[float]:
    """Weight columns by role so descriptions get the room they need."""
    weights = {
        "serial": 0.6, "date": 1.25, "value_date": 1.15, "description": 5.4,
        "reference": 1.5, "debit": 1.35, "credit": 1.35, "balance": 1.5,
        "amount": 1.6, "branch": 0.8, "points": 0.9,
    }
    raw = [weights.get(role, 1.0) for role in statement.spec.roles]
    total = sum(raw)
    return [available * value / total for value in raw]


def _draw_header(
    canvas: pdfcanvas.Canvas, statement: GeneratedStatement, *, first_page: bool
) -> float:
    """Draw the masthead; return the y-coordinate where the table may start."""
    spec = statement.spec
    y = PAGE_HEIGHT - MARGIN

    canvas.setFillColor(INK)
    canvas.setFont("Helvetica-Bold", 12 if first_page else 9)
    canvas.drawString(MARGIN, y - 10, spec.header_lines[0])
    y -= 16 if first_page else 13

    canvas.setFont("Helvetica", 8.5 if first_page else 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(MARGIN, y - 8, spec.header_lines[1])
    y -= 18

    if not first_page:
        canvas.setFont("Helvetica", 7)
        canvas.drawString(
            MARGIN, y - 6,
            f"{spec.period_label} {statement.period_start:%d/%m/%Y} to "
            f"{statement.period_end:%d/%m/%Y}   |   "
            f"Account No: XXXXXXXX{statement.account_last4}",
        )
        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(0.4)
        canvas.line(MARGIN, y - 12, PAGE_WIDTH - MARGIN, y - 12)
        return y - 20

    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.6)
    canvas.line(MARGIN, y, PAGE_WIDTH - MARGIN, y)
    y -= 12

    left = [
        statement.holder_name,
        *[part.strip() for part in statement.holder_address.split(",")],
    ]
    is_card = statement.total_due is not None
    if is_card:
        right = [
            f"Card Number: {statement.card_masked}",
            f"{spec.period_label}: {statement.period_start:%d/%m/%Y} - {statement.period_end:%d/%m/%Y}",
            f"Payment Due Date: {statement.payment_due_date:%d/%m/%Y}",
            f"Credit Limit: {format_inr(statement.credit_limit or Decimal('0'))}",
            f"Total Amount Due: {format_inr(statement.total_due or Decimal('0'))}",
            f"Minimum Amount Due: {format_inr(statement.minimum_due or Decimal('0'))}",
        ]
    else:
        right = [
            f"Account Number: {statement.account_number}",
            f"IFSC: {statement.ifsc}    Branch: {statement.branch}",
            f"{spec.period_label} {statement.period_start:%d/%m/%Y} to {statement.period_end:%d/%m/%Y}",
            f"{spec.opening_label}: {format_inr(statement.opening_balance)}",
            f"{spec.closing_label}: {format_inr(statement.closing_balance)}",
        ]
        if spec.prints_transaction_count:
            right.append(f"Number of Transactions: {len(statement.entries)}")

    canvas.setFont("Helvetica", 7.4)
    top = y
    for index, line in enumerate(left[:5]):
        canvas.setFillColor(INK if index == 0 else MUTED)
        canvas.drawString(MARGIN, top - 8 - index * 9.5, line)
    for index, line in enumerate(right):
        canvas.setFillColor(MUTED)
        canvas.drawString(PAGE_WIDTH / 2 + 4, top - 8 - index * 9.5, line)

    y = top - 8 - max(len(left[:5]), len(right)) * 9.5 - 8
    canvas.setStrokeColor(RULE)
    canvas.line(MARGIN, y, PAGE_WIDTH - MARGIN, y)
    return y - 8


def _draw_footer(canvas: pdfcanvas.Canvas, statement: GeneratedStatement) -> None:
    canvas.setFont("Helvetica", 6.2)
    canvas.setFillColor(MUTED)
    for index, line in enumerate(statement.spec.footer_lines):
        canvas.drawString(MARGIN, 16 * mm - index * 8, line)


def _row_cells(
    statement: GeneratedStatement, entry: LedgerEntry, serial: int, small: ParagraphStyle,
    right: ParagraphStyle,
) -> list:
    spec = statement.spec
    cells: list = []
    for role in spec.roles:
        if role == "serial":
            cells.append(Paragraph(str(serial), small))
        elif role == "date":
            cells.append(Paragraph(entry.txn_date.strftime(spec.date_format), small))
        elif role == "value_date":
            cells.append(Paragraph(entry.value_date.strftime(spec.date_format), small))
        elif role == "description":
            cells.append(Paragraph(entry.description, small))
        elif role == "reference":
            cells.append(Paragraph(entry.reference[:12] if entry.reference else "", small))
        elif role in {"debit", "credit", "amount"}:
            cells.append(Paragraph(_amount_cell(entry, role, spec.amount_style), right))
        elif role == "balance":
            cells.append(Paragraph(format_inr(entry.balance_after), right))
        elif role == "branch":
            cells.append(Paragraph(statement.branch[:6].upper(), small))
        elif role == "points":
            cells.append(Paragraph(str(max(int(entry.amount / 150), 0)), right))
        else:
            cells.append(Paragraph("", small))
    return cells


def render(statement: GeneratedStatement, path: Path, *, rows_per_page: int = 26) -> int:
    """Write the statement to ``path``. Returns the page count."""
    spec = statement.spec
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas = _StatementCanvas(str(path), pagesize=A4)
    canvas.setTitle("Account Statement")
    canvas.setAuthor(spec.legal_name)
    canvas.setSubject("Statement of Account")

    small = _cell_style(spec.font_size)
    right = _cell_style(spec.font_size, align=TA_RIGHT)
    head = _cell_style(spec.font_size - 0.2, bold=True)
    head_right = _cell_style(spec.font_size - 0.2, bold=True, align=TA_RIGHT)

    header_row = [
        Paragraph(name, head_right if role in {"debit", "credit", "amount", "balance", "points"} else head)
        for name, role in zip(spec.columns, spec.roles)
    ]

    widths = _column_widths(statement, PAGE_WIDTH - 2 * MARGIN)
    is_card = statement.total_due is not None
    has_balance = "balance" in spec.roles

    entries = statement.entries
    chunks: list[list[LedgerEntry]] = []
    first_page_rows = max(rows_per_page - 8, 6)
    cursor = 0
    while cursor < len(entries):
        size = first_page_rows if not chunks else rows_per_page
        chunks.append(entries[cursor:cursor + size])
        cursor += size
    if not chunks:
        chunks = [[]]

    running_serial = 1
    for index, chunk in enumerate(chunks):
        first_page = index == 0
        top = _draw_header(canvas, statement, first_page=first_page)

        rows: list[list] = [header_row]
        highlight: list[int] = []

        if not first_page and has_balance:
            # Brought-forward line. Not a transaction, and a parser that reads
            # it as one gains a phantom row on every page after the first.
            carried = chunks[index - 1][-1].balance_after if chunks[index - 1] else statement.opening_balance
            rows.append(_balance_marker(statement, "B/F", carried, small, right))
            highlight.append(len(rows) - 1)
        elif first_page and has_balance and not is_card:
            rows.append(
                _balance_marker(statement, spec.opening_label, statement.opening_balance, small, right)
            )
            highlight.append(len(rows) - 1)

        for entry in chunk:
            rows.append(_row_cells(statement, entry, running_serial, small, right))
            running_serial += 1

        last_page = index == len(chunks) - 1
        if has_balance and not is_card:
            label = spec.closing_label if last_page else "C/F"
            closing = chunk[-1].balance_after if chunk else statement.opening_balance
            rows.append(_balance_marker(statement, label, closing, small, right))
            highlight.append(len(rows) - 1)

        table = Table(rows, colWidths=widths, repeatRows=0)
        table.setStyle(_table_style(spec, highlight))
        _, height = table.wrapOn(canvas, PAGE_WIDTH - 2 * MARGIN, top - 25 * mm)
        table.drawOn(canvas, MARGIN, top - height)

        if last_page and is_card:
            _draw_card_summary(canvas, statement, top - height - 14)

        _draw_footer(canvas, statement)
        canvas.showPage()

    canvas.save()
    return len(chunks)


def _balance_marker(
    statement: GeneratedStatement, label: str, value: Decimal,
    small: ParagraphStyle, right: ParagraphStyle,
) -> list:
    """A balance line that is not a transaction."""
    cells: list = []
    for role in statement.spec.roles:
        if role == "description":
            cells.append(Paragraph(f"<b>{label}</b>", small))
        elif role == "balance":
            cells.append(Paragraph(f"<b>{format_inr(value)}</b>", right))
        else:
            cells.append(Paragraph("", small))
    return cells


def _table_style(spec, highlight: list[int]) -> TableStyle:
    commands = [
        ("GRID", (0, 0), (-1, -1), 0.25, RULE),
        ("BACKGROUND", (0, 0), (-1, 0), BAND),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 2.4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.4),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ]
    for row in highlight:
        commands.append(("BACKGROUND", (0, row), (-1, row), BAND))
    return TableStyle(commands)


def _draw_card_summary(canvas: pdfcanvas.Canvas, statement: GeneratedStatement, y: float) -> None:
    """The account-summary block a card statement must reconcile against."""
    canvas.setFont("Helvetica-Bold", 7.6)
    canvas.setFillColor(INK)
    canvas.drawString(MARGIN, y, "Account Summary")

    purchases = sum(
        (entry.amount for entry in statement.entries if entry.direction == Direction.DEBIT),
        Decimal("0.00"),
    )
    credits = sum(
        (entry.amount for entry in statement.entries if entry.direction == Direction.CREDIT),
        Decimal("0.00"),
    )
    lines = (
        ("Previous Balance", statement.opening_balance),
        ("Purchases & Other Charges", purchases),
        ("Payments & Credits", credits),
        ("Total Amount Due", statement.total_due or Decimal("0.00")),
        ("Minimum Amount Due", statement.minimum_due or Decimal("0.00")),
    )
    canvas.setFont("Helvetica", 7.2)
    for index, (label, value) in enumerate(lines):
        row_y = y - 11 - index * 9.5
        canvas.setFillColor(MUTED)
        canvas.drawString(MARGIN + 2, row_y, label)
        canvas.setFillColor(INK)
        canvas.drawRightString(MARGIN + 150, row_y, format_inr(value))


def rasterise(source: Path, target: Path, *, dpi: int = 180) -> None:
    """Turn a text-layer PDF into a scanned-looking one with no text layer.

    This is how the OCR fallback gets a fixture whose correct answer is known
    exactly — the same ledger, rendered twice, read two different ways.
    """
    import fitz

    with fitz.open(str(source)) as document:
        output = fitz.open()
        for page in document:
            pixmap = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
            rect = fitz.Rect(0, 0, page.rect.width, page.rect.height)
            new_page = output.new_page(width=page.rect.width, height=page.rect.height)
            new_page.insert_image(rect, stream=pixmap.tobytes("png"))
        target.parent.mkdir(parents=True, exist_ok=True)
        output.save(str(target))
        output.close()
