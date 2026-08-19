"""Statement and job schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class UploadFileResult(BaseModel):
    filename: str
    accepted: bool
    statement_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    page_count: int = 0
    error_code: str | None = None
    message: str | None = None


class UploadResponse(BaseModel):
    """A batch result. Files are reported individually rather than the whole
    upload succeeding or failing — one unreadable PDF among twelve should not
    discard the other eleven."""

    accepted: int
    rejected: int
    results: list[UploadFileResult]


class StatementSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    bank_code: str | None = None
    bank_name: str | None = None
    account_type: str | None = None
    account_last4: str | None = None
    document_type: str
    status: str
    trust_status: str
    period_start: date | None = None
    period_end: date | None = None
    page_count: int | None = None
    #: Rows that reached the trusted ledger.
    transaction_count: int = 0
    #: Rows the parser read. Deliberately a separate number: a statement can be
    #: fully read and still hold nothing in the ledger, because reconciliation
    #: and duplicate detection decide what is allowed in.
    extracted_transaction_count: int = 0
    duplicate_count: int = 0
    file_size_bytes: int
    created_at: datetime
    processed_at: datetime | None = None
    error_code: str | None = None
    job_id: uuid.UUID | None = None
    job_state: str | None = None
    progress: int | None = None


class StatementDetail(StatementSummary):
    extraction_method: str | None = None
    # Money as a string end to end: the database holds NUMERIC and the client
    # groups digits itself, so no value ever passes through a float.
    opening_balance: str | None = None
    closing_balance: str | None = None
    parser_name: str | None = None
    parser_version: str | None = None


class StatementHealthResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    statement_id: uuid.UUID
    reconciles: bool
    reconciliation_delta_paise: int | None = None
    balance_continuous: bool
    first_divergent_row: int | None = None
    first_divergent_page: int | None = None
    pages_continuous: bool
    declared_transaction_count: int | None = None
    extracted_transaction_count: int = 0
    ocr_page_count: int = 0
    total_page_count: int = 0
    avg_confidence_extraction: Decimal | None = None
    avg_confidence_merchant: Decimal | None = None
    avg_confidence_category: Decimal | None = None
    avg_confidence_validation: Decimal | None = None
    checks: dict[str, Any] | None = None
    updated_at: datetime | None = None


class JobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    statement_id: uuid.UUID | None = None
    state: str
    progress: int
    attempt: int
    error_code: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    summary: dict[str, Any] | None = None


class DownloadUrlResponse(BaseModel):
    """A short-lived, single-statement download link.

    Not an object-store presigned URL: statements are encrypted by the
    application, so a direct link would hand the browser ciphertext. The link
    points back at the API, which authorises and decrypts.
    """

    url: str
    expires_in: int = Field(description="Seconds until the link stops working")
