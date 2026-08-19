"""The canonical transaction schema.

Every parser — bank-specific, generic, credit-card, and every future non-PDF
ingestion source — produces exactly this shape. Downstream code (reconciliation,
deduplication, movement classification, the ledger writer, the accuracy harness)
is written against this and nothing else, so adding a bank never touches
anything past the parser.

Two rules are enforced here rather than trusted to callers:

**Amounts are always positive.** Sign lives in ``direction``. A statement that
prints ``-1,250.00`` in a single amount column and one that prints ``1,250.00``
in a Withdrawal column mean the same thing, and the rest of the system should
never have to know which layout it came from.

**Money is ``Decimal``, never ``float``.** ``0.1 + 0.2`` is not ``0.3`` in
binary floating point, and a reconciliation check that must land on exactly
₹0.00 cannot be built on a type that cannot represent ₹0.10. The constructor
rejects floats outright — passing one is a bug, and a silent conversion would
hide it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from app.models.enums import (
    CategorySource,
    Direction,
    DocumentType,
    MovementType,
    PaymentMethod,
)

PARSER_SCHEMA_VERSION = "1"

PAISE = Decimal("0.01")


def to_money(value: Any, *, field_name: str = "amount") -> Decimal:
    """Coerce to an exact rupee Decimal, refusing float.

    ``float`` is rejected rather than converted because the conversion is
    lossy at the point it happens — by the time the value arrives here the
    damage is already done, and quietly accepting it would make a reconciliation
    failure look like a parser bug rather than a type bug.
    """
    if isinstance(value, float):
        raise TypeError(
            f"{field_name} was a float; money must be Decimal or str "
            "(see parsers.canonical.to_money)"
        )
    if isinstance(value, Decimal):
        return value.quantize(PAISE)
    return Decimal(str(value)).quantize(PAISE)


@dataclass(slots=True)
class CanonicalTransaction:
    """One row of a statement, in the only shape the ledger accepts."""

    txn_date: date
    description: str
    amount: Decimal
    direction: Direction

    value_date: date | None = None
    balance_after: Decimal | None = None
    reference: str | None = None

    # Filled by the merchant normalizer, not by the bank parser. A parser's job
    # is to read the row correctly; working out that "UPI-SWIGGY@YBL-527..." is
    # Swiggy is a separate, bank-independent concern.
    merchant_raw: str | None = None
    merchant_normalized: str | None = None
    merchant_slug: str | None = None

    payment_method: PaymentMethod = PaymentMethod.UNKNOWN
    category_slug: str | None = None
    subcategory_slug: str | None = None

    # Which tier of the cascade decided the category, and why. Stored so the
    # "why was this categorised this way?" panel never has to re-derive an
    # answer — a recomputed explanation can disagree with the decision it claims
    # to explain.
    category_source: CategorySource = CategorySource.FALLBACK_OTHER
    category_reason: dict[str, Any] | None = None

    # Set by the deterministic rules. `is_expense` is the flag every analytics
    # query filters on: a credit-card payment settles purchases already counted
    # individually, so counting it too reports double the spending.
    movement_type: MovementType = MovementType.UNKNOWN
    is_expense: bool = True

    # Provenance. `source_page`/`source_row` are what let Statement Health say
    # "the running balance first diverges at row 47 on page 3" instead of
    # "extraction failed".
    source_page: int | None = None
    source_row: int | None = None

    # Per-field extraction confidence, e.g. {"date": 1.0, "amount": 0.97}.
    field_confidence: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.amount = to_money(self.amount)
        if self.amount < 0:
            raise ValueError(
                "amount must be positive; sign belongs to `direction` "
                f"(got {self.amount})"
            )
        if self.balance_after is not None:
            # Balances *may* be negative — an overdrawn account, or a credit
            # card with a payment in credit. Only the transaction amount is
            # sign-free.
            self.balance_after = to_money(self.balance_after, field_name="balance_after")
        self.direction = Direction(self.direction)
        self.payment_method = PaymentMethod(self.payment_method)
        self.movement_type = MovementType(self.movement_type)
        self.category_source = CategorySource(self.category_source)
        self.description = " ".join(self.description.split())

    @property
    def signed_amount(self) -> Decimal:
        """Amount with the direction applied. Used only by arithmetic checks."""
        return -self.amount if self.direction == Direction.DEBIT else self.amount

    def to_json(self) -> dict[str, Any]:
        """Serialise for `expected.json` and the accuracy harness.

        Money crosses as a **string**. JSON has one numeric type and it is a
        double; writing 1234.56 as a JSON number and reading it back is not
        guaranteed to return the same value, which is exactly the guarantee a
        golden fixture exists to provide.
        """
        return {
            "txn_date": self.txn_date.isoformat(),
            "value_date": self.value_date.isoformat() if self.value_date else None,
            "description": self.description,
            "amount": str(self.amount),
            "direction": str(self.direction),
            "balance_after": str(self.balance_after) if self.balance_after is not None else None,
            "reference": self.reference,
            "merchant_normalized": self.merchant_normalized,
            "merchant_slug": self.merchant_slug,
            "payment_method": str(self.payment_method),
            "category_slug": self.category_slug,
            "subcategory_slug": self.subcategory_slug,
            "category_source": str(self.category_source),
            "movement_type": str(self.movement_type),
            "is_expense": self.is_expense,
            "source_page": self.source_page,
            "source_row": self.source_row,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> CanonicalTransaction:
        return cls(
            txn_date=date.fromisoformat(raw["txn_date"]),
            value_date=date.fromisoformat(raw["value_date"]) if raw.get("value_date") else None,
            description=raw["description"],
            amount=Decimal(raw["amount"]),
            direction=Direction(raw["direction"]),
            balance_after=(
                Decimal(raw["balance_after"]) if raw.get("balance_after") is not None else None
            ),
            reference=raw.get("reference"),
            merchant_normalized=raw.get("merchant_normalized"),
            merchant_slug=raw.get("merchant_slug"),
            payment_method=PaymentMethod(raw.get("payment_method", PaymentMethod.UNKNOWN)),
            category_slug=raw.get("category_slug"),
            subcategory_slug=raw.get("subcategory_slug"),
            category_source=CategorySource(
                raw.get("category_source", CategorySource.FALLBACK_OTHER)
            ),
            movement_type=MovementType(raw.get("movement_type", MovementType.UNKNOWN)),
            is_expense=raw.get("is_expense", True),
            source_page=raw.get("source_page"),
            source_row=raw.get("source_row"),
        )


@dataclass(slots=True)
class StatementMetadata:
    """What the statement says about itself.

    Every field is optional because every field is genuinely missing from some
    real statement. A parser that invents a closing balance to satisfy a type
    signature would turn "we could not read this" into a reconciliation the
    system believes.
    """

    bank_code: str | None = None
    bank_name: str | None = None
    document_type: DocumentType = DocumentType.UNKNOWN
    account_last4: str | None = None
    account_type: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    opening_balance: Decimal | None = None
    closing_balance: Decimal | None = None

    # Some statements print "Total 47 transactions". When present it is an
    # independent check on recall that does not depend on our own extraction.
    declared_transaction_count: int | None = None

    # Card statements only.
    total_amount_due: Decimal | None = None
    minimum_amount_due: Decimal | None = None
    payment_due_date: date | None = None
    credit_limit: Decimal | None = None

    def __post_init__(self) -> None:
        for name in (
            "opening_balance", "closing_balance", "total_amount_due",
            "minimum_amount_due", "credit_limit",
        ):
            value = getattr(self, name)
            if value is not None:
                setattr(self, name, to_money(value, field_name=name))
        self.document_type = DocumentType(self.document_type)

    def to_json(self) -> dict[str, Any]:
        def money(value: Decimal | None) -> str | None:
            return str(value) if value is not None else None

        return {
            "bank_code": self.bank_code,
            "bank_name": self.bank_name,
            "document_type": str(self.document_type),
            "account_last4": self.account_last4,
            "account_type": self.account_type,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "opening_balance": money(self.opening_balance),
            "closing_balance": money(self.closing_balance),
            "declared_transaction_count": self.declared_transaction_count,
            "total_amount_due": money(self.total_amount_due),
            "minimum_amount_due": money(self.minimum_amount_due),
            "payment_due_date": (
                self.payment_due_date.isoformat() if self.payment_due_date else None
            ),
            "credit_limit": money(self.credit_limit),
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> StatementMetadata:
        def money(key: str) -> Decimal | None:
            value = raw.get(key)
            return Decimal(value) if value is not None else None

        def day(key: str) -> date | None:
            value = raw.get(key)
            return date.fromisoformat(value) if value else None

        return cls(
            bank_code=raw.get("bank_code"),
            bank_name=raw.get("bank_name"),
            document_type=DocumentType(raw.get("document_type", DocumentType.UNKNOWN)),
            account_last4=raw.get("account_last4"),
            account_type=raw.get("account_type"),
            period_start=day("period_start"),
            period_end=day("period_end"),
            opening_balance=money("opening_balance"),
            closing_balance=money("closing_balance"),
            declared_transaction_count=raw.get("declared_transaction_count"),
            total_amount_due=money("total_amount_due"),
            minimum_amount_due=money("minimum_amount_due"),
            payment_due_date=day("payment_due_date"),
            credit_limit=money("credit_limit"),
        )


@dataclass(slots=True)
class ParseResult:
    """What a parser hands back.

    ``warnings`` carries structural observations — a row that could not be
    read, a column that looked ambiguous — as **codes**, never as text
    containing statement content. These end up in logs and on the Statement
    Health screen, both of which are forbidden from carrying financial data.
    """

    metadata: StatementMetadata
    transactions: list[CanonicalTransaction] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    parser_name: str = "unknown"
    parser_version: str = PARSER_SCHEMA_VERSION
    # Rows the parser saw in the table region but could not turn into a
    # transaction. Non-zero is a recall problem and is surfaced, never dropped.
    unparsed_row_count: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": PARSER_SCHEMA_VERSION,
            "parser_name": self.parser_name,
            "parser_version": self.parser_version,
            "metadata": self.metadata.to_json(),
            "transactions": [txn.to_json() for txn in self.transactions],
            "warnings": self.warnings,
            "unparsed_row_count": self.unparsed_row_count,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> ParseResult:
        return cls(
            metadata=StatementMetadata.from_json(raw["metadata"]),
            transactions=[CanonicalTransaction.from_json(t) for t in raw["transactions"]],
            warnings=list(raw.get("warnings", [])),
            parser_name=raw.get("parser_name", "unknown"),
            parser_version=raw.get("parser_version", PARSER_SCHEMA_VERSION),
            unparsed_row_count=raw.get("unparsed_row_count", 0),
        )
