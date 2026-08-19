"""Transaction API contracts.

**Money crosses as a string.** JSON has one numeric type and it is a double, so
serialising ₹1,23,456.78 as a JSON number and reading it back is not guaranteed
to return the same value. The database holds NUMERIC, Python holds Decimal, the
wire holds a string, and the browser formats it with `Intl.NumberFormat('en-IN')`
— float never appears anywhere in that chain.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class ConfidenceBreakdown(BaseModel):
    extraction: Decimal
    merchant: Decimal
    category: Decimal
    validation: Decimal
    minimum: Decimal
    weakest: str | None = None

    @field_serializer("extraction", "merchant", "category", "validation", "minimum")
    def _as_string(self, value: Decimal) -> str:
        return str(value)


class TransactionSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    account_id: uuid.UUID
    statement_id: uuid.UUID | None = None

    txn_date: date
    value_date: date | None = None
    description: str
    amount: Decimal
    direction: str
    merchant: str | None = None
    payment_method: str
    balance_after: Decimal | None = None

    category_slug: str | None = None
    category_name: str | None = None
    category_color: str | None = None
    subcategory_slug: str | None = None
    subcategory_name: str | None = None
    category_source: str

    movement_type: str
    is_expense: bool
    transfer_group_id: uuid.UUID | None = None

    confidence_extraction: Decimal
    confidence_merchant: Decimal
    confidence_category: Decimal
    confidence_validation: Decimal
    confidence_min: Decimal
    review_status: str
    is_verified: bool

    bank_code: str | None = None
    bank_name: str | None = None
    account_last4: str | None = None
    statement_trust_status: str | None = None

    source_page: int | None = None
    source_row: int | None = None
    created_at: datetime

    @field_serializer(
        "amount", "balance_after", "confidence_extraction", "confidence_merchant",
        "confidence_category", "confidence_validation", "confidence_min",
    )
    def _money_and_scores_as_strings(self, value: Decimal | None) -> str | None:
        return None if value is None else str(value)


class TransactionDetail(TransactionSummary):
    """Adds what the statement originally said, beside the effective values."""

    original_txn_date: date
    original_description: str
    original_amount: Decimal
    original_direction: str
    original_merchant: str | None = None
    field_confidence: dict[str, Any] | None = None
    category_reason: dict[str, Any] | None = None
    verified_at: datetime | None = None

    @field_serializer("original_amount")
    def _original_amount_as_string(self, value: Decimal) -> str:
        return str(value)


class TransactionPage(BaseModel):
    items: list[TransactionSummary]
    total: int
    limit: int
    offset: int


class TransactionCorrection(BaseModel):
    """A user's correction. Every field is optional; absent means unchanged."""

    txn_date: date | None = None
    description: str | None = Field(default=None, max_length=2000)
    amount: Decimal | None = Field(default=None, ge=0)
    direction: str | None = None
    merchant: str | None = Field(default=None, max_length=255)
    payment_method: str | None = None
    category_slug: str | None = None
    subcategory_slug: str | None = None
    #: When false the row is corrected without being marked human-verified.
    verify: bool = True


class ExplanationResponse(BaseModel):
    transaction_id: str
    category_slug: str | None
    category_name: str | None
    source: str
    #: A plain sentence, ready to render. Built from what was stored at decision
    #: time rather than re-derived.
    sentence: str
    reason: dict[str, Any]
    confidence: ConfidenceBreakdown
    provenance: dict[str, Any]


class AuditEntry(BaseModel):
    field_name: str
    old_value: str | None
    new_value: str | None
    actor_kind: str
    reason: str | None
    changed_at: datetime
    changed_by_name: str | None = None


class ReviewStats(BaseModel):
    review_required: int
    flagged: int
    auto_approved: int
    resolved: int
    total: int
    uncategorised: int


class BulkApproveRequest(BaseModel):
    transaction_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)


class ApplyToSimilarRequest(BaseModel):
    category_slug: str


class AccountSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    bank_code: str
    bank_name: str | None = None
    account_type: str
    status: str
    account_last4: str
    display_name: str | None = None
    current_balance: Decimal | None = None
    balance_as_of: date | None = None
    credit_limit: Decimal | None = None
    coverage_start: date | None = None
    coverage_end: date | None = None
    last_imported_at: datetime | None = None
    transaction_count: int = 0
    statement_count: int = 0
    created_at: datetime

    @field_serializer("current_balance", "credit_limit")
    def _money_as_string(self, value: Decimal | None) -> str | None:
        return None if value is None else str(value)
