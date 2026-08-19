"""The tabular bank-statement parser every other bank parser is built on.

Indian statements differ in vocabulary far more than in structure. Underneath
the column names they are all the same table: a date, a narration, an amount in
one or two columns, and a running balance. So the machinery lives here once, and
a bank parser becomes a declaration of what that bank calls things plus whatever
genuinely deviates.

Two readers, in order:

**Tables.** When extraction found a grid, columns are mapped by header text and
rows are read positionally. Continuation pages usually reprint no header, so the
mapping carries forward.

**Text lines.** When there is no usable grid — which is always the case for an
OCR'd scan — rows are read from raw lines: a line beginning with a date starts a
transaction, and the trailing numbers are its amounts. Direction then comes from
the *running balance delta* rather than from column position, which is the one
signal a scan cannot smear: if the balance fell, it was a debit.

Rows that are not transactions are the recurring hazard. "B/F", "Opening
Balance", "Total" and "C/F" all carry a date-shaped or amount-shaped payload,
and a parser that reads them gains a phantom transaction on every page. They are
rejected explicitly, and the rejection is tested.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from app.models.enums import Direction, DocumentType

from parsers.base import BankParser
from parsers.canonical import CanonicalTransaction, ParseResult, StatementMetadata
from parsers.document import ExtractedDocument, Table
from parsers.normalizers import dates as datenorm
from parsers.normalizers import text as textnorm
from parsers.normalizers.amount import AmountParseError, parse_amount
from parsers.registry import registry

# --------------------------------------------------------------------------- #
# Column vocabulary
#
# Deliberately *not* shared with the fixture generator. If both sides read from
# one table, a header the parser cannot recognise would still line up, and the
# accuracy harness would never see the bug.
# --------------------------------------------------------------------------- #

COLUMN_SYNONYMS: dict[str, tuple[str, ...]] = {
    "date": (
        "date", "txn date", "tran date", "transaction date", "trans date",
        "tran. date", "date of transaction", "post date", "posting date",
        "txn dt", "tran dt", "date of txn",
    ),
    "value_date": ("value date", "value dt", "val date", "value dt.", "val dt"),
    "description": (
        "narration", "description", "particulars", "transaction remarks",
        "remarks", "transaction description", "transaction details", "details",
        "transaction", "narrative", "transaction particulars", "description of transaction",
    ),
    "reference": (
        "chq./ref.no.", "chq/ref no", "ref no./cheque no.", "cheque number",
        "chq no", "cheque no", "ref no", "reference", "chq no.", "instrument no",
        "utr", "ref/chq no", "cheque/ref no", "chq./ref. no.", "ref no.",
    ),
    "debit": (
        "withdrawal amt.", "withdrawal amount (inr)", "withdrawal", "withdrawals",
        "debit", "debit amount", "dr", "paid out", "withdrawal (dr)",
        "amount withdrawn", "withdrawal amt", "debits", "debit (inr)",
    ),
    "credit": (
        "deposit amt.", "deposit amount (inr)", "deposit", "deposits", "credit",
        "credit amount", "cr", "paid in", "deposit (cr)", "amount deposited",
        "deposit amt", "credits", "credit (inr)",
    ),
    "balance": (
        "closing balance", "balance", "balance (inr)", "running balance",
        "balance amount", "available balance", "closing bal", "bal",
    ),
    "amount": (
        "amount", "amount (in rs.)", "amount (in rs)", "amount(inr)",
        "transaction amount", "amount inr", "amount (rs.)", "amt",
    ),
    "serial": ("s no.", "sr no", "sl no", "serial", "serno.", "s.no", "#", "sno"),
    "branch": ("init.br", "init br", "branch", "init.br."),
    "points": ("reward points", "points", "reward point"),
}

_HEADER_LOOKUP: dict[str, str] = {
    synonym: role for role, synonyms in COLUMN_SYNONYMS.items() for synonym in synonyms
}

# Lines and rows that carry balances but are not transactions.
_NON_TRANSACTION = re.compile(
    r"^\s*(?:b\s*/\s*f|c\s*/\s*f|bal\s*(?:b|c)/f|brought\s+forward|carried\s+forward"
    r"|opening\s+bal(?:ance)?|closing\s+bal(?:ance)?|balance\s+(?:b|c)/f"
    r"|total|sub\s*total|grand\s+total|statement\s+summary|summary"
    r"|continued|page\s+\d+|transaction\s+total)\s*[:\-]?\s*$",
    re.IGNORECASE,
)

# The separator tolerance is not cosmetic. Tesseract routinely inserts a stray
# space beside a group separator on small type — "2,22 ,838.23" — and a pattern
# that stops at the space reads the balance as two numbers, takes "2,22" as the
# transaction amount, and yields ₹222.00 instead of ₹2,22,838.23. The corrupted
# running balance then inverts the direction of every row after it, so one
# tokenisation gap becomes a statement-wide failure. Capped at two spaces:
# genuine column gaps in these layouts are far wider, so this cannot weld two
# adjacent columns together.
_MONEY_TOKEN = re.compile(
    r"(?<![\d.])(\d{1,3}(?:[ ]{0,2},[ ]{0,2}\d{2,3})*(?:\.\d{1,2})?|\d+\.\d{1,2})"
    r"\s*(Dr|Cr|DR|CR)?(?![\d])"
)


@dataclass(slots=True)
class ColumnMap:
    """Which table column holds which field."""

    columns: dict[str, int]

    def get(self, role: str) -> int | None:
        return self.columns.get(role)

    @property
    def usable(self) -> bool:
        has_date = "date" in self.columns
        has_money = any(role in self.columns for role in ("debit", "credit", "amount"))
        has_text = "description" in self.columns
        return has_date and has_money and has_text


def _normalise_header(cell: str) -> str:
    return re.sub(r"\s+", " ", textnorm.collapse(cell)).strip().lower().rstrip(":")


def map_header_row(row: list[str]) -> ColumnMap | None:
    """Map a candidate header row to field roles."""
    mapping: dict[str, int] = {}
    for index, cell in enumerate(row):
        key = _normalise_header(cell)
        if not key:
            continue
        role = _HEADER_LOOKUP.get(key)
        if role is None:
            # Header text is sometimes wrapped or suffixed ("Withdrawal\nAmt.",
            # "Debit Amount (INR)"). Fall back to the longest synonym contained
            # in the cell, longest-first so "value date" beats "date".
            candidates = [
                (synonym, mapped)
                for synonym, mapped in _HEADER_LOOKUP.items()
                if synonym in key
            ]
            if not candidates:
                continue
            role = max(candidates, key=lambda pair: len(pair[0]))[1]
        mapping.setdefault(role, index)

    column_map = ColumnMap(columns=mapping)
    return column_map if column_map.usable else None


class TabularBankParser(BankParser):
    """Reads a statement from a column-mapped table, or from text lines."""

    parser_name = "generic_bank"
    bank_code = "GENERIC"
    bank_name = "Unidentified bank"
    document_types = (DocumentType.BANK_STATEMENT,)
    priority = 0  # the fallback; bank parsers all outrank it

    #: What a bare amount means in a single-column layout. ``None`` makes an
    #: unmarked cell an error, which is right for a bank statement where
    #: direction is always printed. Card statements set this to DEBIT, because
    #: they genuinely print purchases unmarked and only suffix credits with
    #: ``Cr`` — the convention that silently doubles someone's spending if a
    #: parser treats an unmarked row as a credit.
    single_column_default: Direction | None = None

    #: Labels this bank prints beside the opening and closing balance.
    opening_labels: tuple[str, ...] = (
        r"opening\s+balance", r"balance\s+b/f", r"brought\s+forward",
        r"opening\s+bal",
    )
    closing_labels: tuple[str, ...] = (
        r"closing\s+balance", r"balance\s+c/f", r"carried\s+forward",
        r"closing\s+bal",
    )

    # ------------------------------------------------------------- metadata --

    def read_metadata(self, document: ExtractedDocument) -> StatementMetadata:
        header = document.header_text(2)
        period_start, period_end = self.find_period(header)

        metadata = StatementMetadata(
            bank_code=self.bank_code,
            bank_name=self.bank_name,
            document_type=DocumentType.BANK_STATEMENT,
            account_last4=self.find_last4(header),
            period_start=period_start,
            period_end=period_end,
            opening_balance=self.find_money_label(header, *self.opening_labels),
            closing_balance=self.find_money_label(header, *self.closing_labels),
        )

        count = re.search(
            r"(?:number\s+of\s+transactions|total\s+transactions|transaction\s+count)"
            r"\s*[:\-]?\s*(\d{1,5})",
            header,
            re.IGNORECASE,
        )
        if count:
            metadata.declared_transaction_count = int(count.group(1))

        return metadata

    # ---------------------------------------------------------------- parse --

    def parse(self, document: ExtractedDocument) -> ParseResult:
        metadata = self.read_metadata(document)
        result = self._result(metadata)

        transactions, unparsed = self._parse_tables(document, metadata)

        # A table read that produced nothing, or that plainly lost rows, falls
        # back to text lines. Both readers are tried rather than trusting the
        # first, because a grid that extracts as two columns yields a small
        # number of confidently wrong rows — which is worse than none.
        if len(transactions) < 3:
            line_transactions, line_unparsed = self._parse_text_lines(document, metadata)
            if len(line_transactions) > len(transactions):
                transactions, unparsed = line_transactions, line_unparsed
                result.warnings.append("read_from_text_lines")
                result.warnings.extend(getattr(self, "_line_warnings", []))

        self.enrich(transactions)
        result.transactions = transactions
        result.unparsed_row_count = unparsed

        if metadata.opening_balance is None and transactions:
            # Derive it from the first row's balance so reconciliation still has
            # something to check. Recorded as a warning: a derived opening
            # balance cannot independently confirm the statement's arithmetic.
            first = transactions[0]
            if first.balance_after is not None:
                metadata.opening_balance = first.balance_after - first.signed_amount
                result.warnings.append("opening_balance_derived")

        if metadata.closing_balance is None and transactions:
            last = transactions[-1]
            if last.balance_after is not None:
                metadata.closing_balance = last.balance_after
                result.warnings.append("closing_balance_derived")

        return result

    # --------------------------------------------------------- table reader --

    def _parse_tables(
        self, document: ExtractedDocument, metadata: StatementMetadata
    ) -> tuple[list[CanonicalTransaction], int]:
        transactions: list[CanonicalTransaction] = []
        unparsed = 0
        column_map: ColumnMap | None = None
        year_hint = self.period_year(metadata)

        for page in document.pages:
            for table in page.tables:
                page_map, start_index = self._locate_header(table)
                if page_map is not None:
                    column_map = page_map
                if column_map is None:
                    continue

                for row_index in range(start_index, len(table.rows)):
                    row = table.rows[row_index]
                    parsed = self._row_to_transaction(
                        row, column_map, page.page_number, row_index, year_hint,
                        transactions[-1] if transactions else None,
                    )
                    if parsed is _CONTINUATION:
                        self._append_continuation(transactions, row, column_map)
                    elif parsed is _SKIP:
                        continue
                    elif parsed is None:
                        unparsed += 1
                    else:
                        transactions.append(parsed)

        return transactions, unparsed

    @staticmethod
    def _locate_header(table: Table) -> tuple[ColumnMap | None, int]:
        """Find the header row; return the mapping and the first data row index."""
        for index, row in enumerate(table.rows[:4]):
            mapping = map_header_row(row)
            if mapping is not None:
                return mapping, index + 1
        return None, 0

    def _row_to_transaction(
        self,
        row: list[str],
        column_map: ColumnMap,
        page_number: int,
        row_index: int,
        year_hint: int | None,
        previous: CanonicalTransaction | None,
    ):
        def cell(role: str) -> str:
            index = column_map.get(role)
            if index is None or index >= len(row):
                return ""
            return textnorm.collapse(row[index])

        description = cell("description")
        date_text = cell("date")

        if _NON_TRANSACTION.match(description):
            return _SKIP
        if not any(value.strip() for value in row):
            return _SKIP

        if not date_text:
            # No date: either a wrapped continuation of the row above, or a
            # summary line. A continuation carries description text and no money.
            has_money = any(cell(role) for role in ("debit", "credit", "amount"))
            if description and not has_money and previous is not None:
                return _CONTINUATION
            return _SKIP

        try:
            txn_date = datenorm.parse_date(date_text, year_hint=year_hint)
        except datenorm.DateParseError:
            return None

        amount, direction = self._read_amounts(cell, previous)
        if amount is None or direction is None:
            return None

        balance = None
        balance_text = cell("balance")
        if balance_text:
            try:
                balance = parse_amount(balance_text)
            except AmountParseError:
                balance = None

        value_date = None
        value_text = cell("value_date")
        if value_text:
            try:
                value_date = datenorm.parse_date(value_text, year_hint=year_hint)
            except datenorm.DateParseError:
                value_date = None

        return CanonicalTransaction(
            txn_date=txn_date,
            value_date=value_date,
            description=description,
            amount=amount,
            direction=direction,
            balance_after=balance,
            reference=(cell("reference") or None),
            source_page=page_number,
            source_row=row_index,
            field_confidence={"date": 1.0, "amount": 1.0, "direction": 1.0},
        )

    def _read_amounts(self, cell, previous: CanonicalTransaction | None):
        """Resolve amount and direction from whichever columns this bank uses."""
        debit_text, credit_text = cell("debit"), cell("credit")

        if debit_text or credit_text:
            for text, direction in ((debit_text, Direction.DEBIT),
                                    (credit_text, Direction.CREDIT)):
                if not text:
                    continue
                try:
                    value = parse_amount(text)
                except AmountParseError:
                    continue
                if value != 0:
                    return abs(value), direction
            return None, None

        amount_text = cell("amount")
        if not amount_text:
            return None, None

        # One amount column. Direction comes from an explicit Dr/Cr suffix, a
        # sign, or — failing both — the running balance, which is the only
        # remaining evidence and is exact when it is present.
        from parsers.normalizers.amount import parse_amount_with_direction

        try:
            return parse_amount_with_direction(
                amount_text, default=self.single_column_default
            )
        except AmountParseError:
            pass

        try:
            value = parse_amount(amount_text)
        except AmountParseError:
            return None, None

        balance_text = cell("balance")
        if balance_text and previous is not None and previous.balance_after is not None:
            try:
                balance = parse_amount(balance_text)
            except AmountParseError:
                balance = None
            if balance is not None:
                delta = balance - previous.balance_after
                if delta != 0:
                    return abs(value), Direction.DEBIT if delta < 0 else Direction.CREDIT

        return None, None

    @staticmethod
    def _append_continuation(
        transactions: list[CanonicalTransaction], row: list[str], column_map: ColumnMap
    ) -> None:
        index = column_map.get("description")
        if index is None or index >= len(row) or not transactions:
            return
        extra = textnorm.collapse(row[index])
        if extra:
            transactions[-1].description = f"{transactions[-1].description} {extra}".strip()

    # ---------------------------------------------------- text-line reader --

    def _parse_text_lines(
        self, document: ExtractedDocument, metadata: StatementMetadata
    ) -> tuple[list[CanonicalTransaction], int]:
        """Read rows from raw text, using balance movement to decide direction.

        This is the OCR path and the ruled-table-failure path. It is more
        forgiving than the table reader and correspondingly less certain, so
        every transaction it produces carries lower extraction confidence.
        """
        transactions: list[CanonicalTransaction] = []
        unparsed = 0
        year_hint = self.period_year(metadata)
        running = metadata.opening_balance
        self_warnings = self._line_warnings = []

        for page_number, line in document.all_lines():
            token = datenorm.leading_date(line)
            if token is None:
                if transactions and self._is_continuation_line(line):
                    transactions[-1].description = (
                        f"{transactions[-1].description} {textnorm.collapse(line)}".strip()
                    )
                continue

            remainder = line[line.index(token) + len(token):]
            if _NON_TRANSACTION.match(textnorm.collapse(remainder)):
                continue

            try:
                txn_date = datenorm.parse_date(token, year_hint=year_hint)
            except datenorm.DateParseError:
                unparsed += 1
                continue

            money = _MONEY_TOKEN.findall(remainder)
            if not money:
                continue

            parsed = self._from_money_tokens(money, running)
            if parsed is None:
                unparsed += 1
                continue
            amount, direction, balance = parsed

            description = _MONEY_TOKEN.sub(" ", remainder)
            description = textnorm.collapse(description)
            # A second date on the line is the value date, not description.
            description = re.sub(
                r"^\s*\d{1,2}[-/. ](?:\d{1,2}|[A-Za-z]{3,9})[-/. ]\d{2,4}\s*", "", description
            ).strip()

            if not description:
                unparsed += 1
                continue

            confidence = {"date": 0.95, "amount": 0.9, "direction": 0.9}
            # Cross-check: the balance should have moved by exactly the amount.
            # When it has not, one of the two was misread — most often by OCR —
            # and the row is marked for review rather than quietly trusted.
            if balance is not None and running is not None:
                if abs(balance - running) != amount:
                    confidence["amount"] = 0.5
                    confidence["direction"] = 0.5
                    if "balance_movement_mismatch" not in self_warnings:
                        self_warnings.append("balance_movement_mismatch")

            transactions.append(
                CanonicalTransaction(
                    txn_date=txn_date,
                    description=description,
                    amount=amount,
                    direction=direction,
                    balance_after=balance,
                    source_page=page_number,
                    source_row=len(transactions),
                    # Lower than the table reader's: column position is evidence,
                    # and this reader does not have it.
                    field_confidence=confidence,
                )
            )
            if balance is not None:
                running = balance

        return transactions, unparsed

    @staticmethod
    def _is_continuation_line(line: str) -> bool:
        stripped = textnorm.collapse(line)
        if not stripped or _NON_TRANSACTION.match(stripped):
            return False
        # A continuation carries narration text and no money of its own.
        return not _MONEY_TOKEN.search(stripped) and len(stripped) > 3

    @staticmethod
    def _from_money_tokens(
        money: list[tuple[str, str]], running: Decimal | None
    ) -> tuple[Decimal, Direction, Decimal | None] | None:
        """Turn the trailing numbers on a line into amount, direction, balance.

        The layouts, in the order they are tested:

        * ``… 1,234.56 Dr 98,765.43``  — suffix carries direction
        * ``… 1,234.56 98,765.43``     — direction from the balance delta
        * ``… 1,234.56``               — no balance; direction is unknowable here
        """
        values: list[tuple[Decimal, str]] = []
        for raw, marker in money:
            try:
                values.append((parse_amount(raw), marker.lower()))
            except AmountParseError:
                continue
        if not values:
            return None

        if len(values) == 1:
            amount, marker = values[0]
            if marker.startswith("c"):
                return abs(amount), Direction.CREDIT, None
            if marker.startswith("d"):
                return abs(amount), Direction.DEBIT, None
            return None

        amount, marker = values[-2]
        balance = values[-1][0]

        if marker.startswith("c"):
            return abs(amount), Direction.CREDIT, balance
        if marker.startswith("d"):
            return abs(amount), Direction.DEBIT, balance

        if running is not None:
            delta = balance - running
            if delta != 0:
                return abs(amount), (Direction.DEBIT if delta < 0 else Direction.CREDIT), balance

        # Three numbers with blanks collapsed: debit, credit and balance cannot
        # be told apart without column geometry. Refusing is correct — guessing
        # here inverts transactions.
        return None


# Sentinels distinguishing "this row is not a transaction" from "this row is a
# transaction we failed to read". Conflating them would let a parser hide its
# recall failures among the page furniture it correctly skipped.
class _Sentinel:
    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.name}>"


_SKIP = _Sentinel("skip")
_CONTINUATION = _Sentinel("continuation")


registry.register(TabularBankParser())
