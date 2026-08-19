"""Writing a statement into the trusted ledger.

This is the gate between "we read a PDF" and "this is what happened to your
money". Everything before it is extraction; everything after it treats these
rows as fact. The order of operations is therefore deliberate:

    resolve account → reconcile → fingerprint → detect duplicates
        → score confidence → insert → record trust → pair movements

**Reconciliation runs before insertion**, because its verdict is an input to
every row's ``confidence_validation``: if the statement does not add up, no
transaction on it can be fully trusted, including the ones that look
immaculate — the misread might be that one.

**Every transaction is written, including the doubtful ones.** A statement that
reconciles must reconcile with all of its transactions present; holding some
back "until reviewed" would leave the ledger unable to reproduce the arithmetic
that made it trustworthy. Doubt is expressed as ``review_status`` and confidence,
never as absence.

**Duplicates are refused by the database.** ``UNIQUE (tenant_id, account_id,
fingerprint)`` with ``ON CONFLICT DO NOTHING`` means a re-uploaded statement
produces zero new rows whether or not the application remembered to check.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.extraction.pipeline import ExtractionOutcome
from app.models.enums import (
    DocumentType,
    ExtractionMethod,
    StatementStatus,
    TrustStatus,
)
from app.services import accounts as account_service
from app.services import confidence as confidence_service
from app.services import fingerprint as fingerprint_service
from app.services import movement as movement_service
from app.services import reconciliation as reconciliation_service

logger = get_logger(__name__)


@dataclass(slots=True)
class LedgerResult:
    account_id: uuid.UUID
    inserted: int = 0
    duplicates: int = 0
    suspected_duplicates: int = 0
    auto_approved: int = 0
    flagged: int = 0
    review_required: int = 0
    transfers_paired: int = 0
    trust_status: TrustStatus = TrustStatus.PENDING
    reconciliation_delta: Decimal | None = None
    report: reconciliation_service.ReconciliationReport = field(
        default_factory=reconciliation_service.ReconciliationReport
    )


async def _category_index(session: AsyncSession) -> tuple[dict[str, uuid.UUID], dict[tuple[str, str], uuid.UUID]]:
    categories = {
        row.slug: row.id
        for row in (await session.execute(text("SELECT slug, id FROM categories"))).all()
    }
    subcategories = {
        (row.category_slug, row.slug): row.id
        for row in (
            await session.execute(
                text(
                    """
                    SELECT s.slug, s.id, c.slug AS category_slug
                    FROM subcategories s JOIN categories c ON c.id = s.category_id
                    """
                )
            )
        ).all()
    }
    return categories, subcategories


async def persist(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    statement_id: uuid.UUID,
    outcome: ExtractionOutcome,
) -> LedgerResult:
    result_set = outcome.result
    metadata = result_set.metadata
    transactions = result_set.transactions

    account_id = await account_service.resolve(
        session,
        tenant_id=tenant_id,
        bank_code=metadata.bank_code,
        bank_name=metadata.bank_name,
        document_type=metadata.document_type,
        account_last4=metadata.account_last4,
        account_type=metadata.account_type,
    )
    result = LedgerResult(account_id=account_id)

    report = reconciliation_service.reconcile(
        metadata, transactions, page_count=len(outcome.document.pages)
    )
    result.report = report
    result.trust_status = report.trust_status
    result.reconciliation_delta = report.delta

    validation_score = confidence_service.statement_validation_score(
        reconciles=report.reconciles,
        unverifiable=report.unverifiable,
        balance_checked=report.balance_checked,
        balance_continuous=report.balance_continuous,
        pages_continuous=report.pages_continuous,
    )

    fingerprints = fingerprint_service.assign(
        transactions, tenant_id=tenant_id, account_id=account_id
    )
    categories, subcategories = await _category_index(session)

    ocr_pages = {
        page.page_number
        for page in outcome.document.pages
        if page.method == ExtractionMethod.OCR
    }

    period_start, period_end = metadata.period_start, metadata.period_end

    for transaction, fingerprint in zip(transactions, fingerprints):
        suspected, evidence = await _suspected_duplicate(
            session, account_id=account_id, transaction=transaction, fingerprint=fingerprint
        )
        if suspected:
            result.suspected_duplicates += 1

        out_of_period = bool(
            period_start and period_end
            and not (period_start <= transaction.txn_date <= period_end)
        )

        scores = confidence_service.score(
            transaction,
            validation=validation_score,
            from_ocr_page=transaction.source_page in ocr_pages,
            suspected_duplicate=suspected,
            out_of_period=out_of_period,
        )

        category_id = categories.get(transaction.category_slug or "")
        subcategory_id = subcategories.get(
            (transaction.category_slug or "", transaction.subcategory_slug or "")
        )

        reason = dict(transaction.category_reason or {})
        if suspected:
            reason["suspected_duplicate"] = evidence
        reason["weakest_dimension"] = scores.weakest

        inserted = await _insert(
            session,
            tenant_id=tenant_id,
            account_id=account_id,
            statement_id=statement_id,
            transaction=transaction,
            fingerprint=fingerprint,
            category_id=category_id,
            subcategory_id=subcategory_id,
            scores=scores,
            reason=reason,
        )

        if not inserted:
            result.duplicates += 1
            continue

        result.inserted += 1
        status = scores.review_status
        if status.value == "auto_approved":
            result.auto_approved += 1
        elif status.value == "flagged":
            result.flagged += 1
        else:
            result.review_required += 1

    await _record_statement(
        session,
        statement_id=statement_id,
        account_id=account_id,
        result=result,
    )
    await _record_health(
        session,
        tenant_id=tenant_id,
        statement_id=statement_id,
        outcome=outcome,
        result=result,
    )

    result.transfers_paired = await movement_service.pair_internal_movements(
        session, tenant_id=tenant_id, statement_id=statement_id
    )

    await account_service.record_statement_coverage(
        session,
        account_id=account_id,
        period_start=period_start,
        period_end=period_end,
        closing_balance=(
            metadata.total_amount_due
            if metadata.document_type == DocumentType.CREDIT_CARD_STATEMENT
            else metadata.closing_balance
        ),
        credit_limit=metadata.credit_limit,
    )

    logger.info(
        "statement_persisted",
        stage="ledger",
        statement_id=str(statement_id),
        account_id=str(account_id),
        count=result.inserted,
        status=str(result.trust_status),
    )
    return result


async def _suspected_duplicate(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    transaction,
    fingerprint: str,
) -> tuple[bool, dict[str, Any] | None]:
    """A same-day, same-amount row already in the ledger with drifted narration.

    Not grounds for dropping the row. Both mistakes cost real money — dropping a
    genuine transaction understates spending with no trace, keeping a true
    duplicate overstates it — so the row is written, flagged, and put in front
    of the one actor who can actually tell the difference.
    """
    candidates = (
        await session.execute(
            text(
                """
                SELECT id, description
                FROM transactions
                WHERE account_id = :account_id
                  AND txn_date = :txn_date
                  AND amount = :amount
                  AND direction = :direction
                  AND fingerprint <> :fingerprint
                LIMIT 5
                """
            ),
            {
                "account_id": account_id,
                "txn_date": transaction.txn_date,
                "amount": transaction.amount,
                "direction": str(transaction.direction),
                "fingerprint": fingerprint,
            },
        )
    ).all()

    for candidate in candidates:
        similar, score = fingerprint_service.looks_like_near_duplicate(
            transaction, candidate.description
        )
        if similar:
            return True, {"transaction_id": str(candidate.id), "similarity": score}
    return False, None


async def _insert(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    statement_id: uuid.UUID,
    transaction,
    fingerprint: str,
    category_id: uuid.UUID | None,
    subcategory_id: uuid.UUID | None,
    scores: confidence_service.Confidence,
    reason: dict[str, Any],
) -> bool:
    """Insert one row. Returns False when the database refused it as a duplicate."""
    row = (
        await session.execute(
            text(
                """
                INSERT INTO transactions (
                    tenant_id, account_id, statement_id,
                    original_txn_date, original_value_date, original_description,
                    original_amount, original_direction, original_balance_after,
                    original_reference, original_merchant, original_payment_method,
                    original_category_id, original_subcategory_id,
                    category_id, subcategory_id, category_source, category_reason,
                    movement_type, is_expense,
                    confidence_extraction, confidence_merchant,
                    confidence_category, confidence_validation,
                    field_confidence, review_status,
                    fingerprint, source_page, source_row, raw_row
                ) VALUES (
                    :tenant_id, :account_id, :statement_id,
                    :txn_date, :value_date, :description,
                    :amount, :direction, :balance_after,
                    :reference, :merchant, :payment_method,
                    :category_id, :subcategory_id,
                    :category_id, :subcategory_id, :category_source,
                    CAST(:category_reason AS jsonb),
                    :movement_type, :is_expense,
                    :c_extraction, :c_merchant, :c_category, :c_validation,
                    CAST(:field_confidence AS jsonb), :review_status,
                    :fingerprint, :source_page, :source_row, CAST(:raw_row AS jsonb)
                )
                ON CONFLICT (tenant_id, account_id, fingerprint) DO NOTHING
                RETURNING id
                """
            ),
            {
                "tenant_id": tenant_id,
                "account_id": account_id,
                "statement_id": statement_id,
                "txn_date": transaction.txn_date,
                "value_date": transaction.value_date,
                "description": transaction.description,
                "amount": transaction.amount,
                "direction": str(transaction.direction),
                "balance_after": transaction.balance_after,
                "reference": transaction.reference,
                "merchant": transaction.merchant_normalized,
                "payment_method": str(transaction.payment_method),
                "category_id": category_id,
                "subcategory_id": subcategory_id,
                "category_source": str(transaction.category_source),
                "category_reason": json.dumps(reason),
                "movement_type": str(transaction.movement_type),
                "is_expense": transaction.is_expense,
                "c_extraction": scores.extraction,
                "c_merchant": scores.merchant,
                "c_category": scores.category,
                "c_validation": scores.validation,
                "field_confidence": json.dumps(transaction.field_confidence or {}),
                "review_status": str(scores.review_status),
                "fingerprint": fingerprint,
                "source_page": transaction.source_page,
                "source_row": transaction.source_row,
                "raw_row": json.dumps(
                    {
                        "merchant_slug": transaction.merchant_slug,
                        "payment_method": str(transaction.payment_method),
                    }
                ),
            },
        )
    ).one_or_none()
    return row is not None


async def _record_statement(
    session: AsyncSession,
    *,
    statement_id: uuid.UUID,
    account_id: uuid.UUID,
    result: LedgerResult,
) -> None:
    await session.execute(
        text(
            """
            UPDATE statements
            SET account_id = :account_id,
                trust_status = :trust_status,
                transaction_count = :count,
                duplicate_count = :duplicates,
                status = :status,
                processed_at = now()
            WHERE id = :id
            """
        ),
        {
            "id": statement_id,
            "account_id": account_id,
            "trust_status": str(result.trust_status),
            "count": result.inserted,
            "duplicates": result.duplicates,
            "status": str(StatementStatus.PROCESSED),
        },
    )


async def _record_health(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    statement_id: uuid.UUID,
    outcome: ExtractionOutcome,
    result: LedgerResult,
) -> None:
    report = result.report
    metadata = outcome.result.metadata
    total_pages = len(outcome.document.pages)
    ocr_pages = outcome.document.ocr_page_count
    extracted = len(outcome.result.transactions)
    declared = metadata.declared_transaction_count

    if report.unverifiable:
        reconciliation_check = {
            "status": "warn",
            "note": "the statement did not print both balances, so its "
                    "arithmetic could not be checked",
        }
    elif report.reconciles:
        reconciliation_check = {"status": "pass", "delta_paise": 0}
    else:
        reconciliation_check = {
            "status": "fail",
            "delta_paise": int(report.delta * 100),
            "first_divergent_row": report.first_divergent_row,
            "first_divergent_page": report.first_divergent_page,
        }

    checks = {
        "structural_validation": {"status": "pass"},
        "document_classification": {
            "status": "pass" if outcome.document_type != DocumentType.UNKNOWN else "warn",
            "detected": str(outcome.document_type),
            "confidence": outcome.classification_confidence,
        },
        "bank_detection": {
            "status": "pass" if not outcome.dispatch.is_fallback else "warn",
            "detected": metadata.bank_code or "unknown",
            "parser": outcome.result.parser_name,
            "confidence": outcome.dispatch.confidence,
            "note": "read with the generic parser" if outcome.dispatch.is_fallback else None,
        },
        "text_layer": {
            "status": "pass" if ocr_pages == 0 else "warn",
            "ocr_pages": ocr_pages,
            "total_pages": total_pages,
        },
        "extraction": {
            "status": "fail" if extracted == 0
            else "warn" if (declared is not None and declared != extracted)
            else "pass",
            "extracted": extracted,
            "declared": declared,
            "unparsed_rows": outcome.result.unparsed_row_count,
        },
        "reconciliation": reconciliation_check,
        "duplicates": {
            "status": "pass" if not result.duplicates and not result.suspected_duplicates
            else "warn",
            "exact": result.duplicates,
            "suspected": result.suspected_duplicates,
        },
    }

    await session.execute(
        text(
            """
            INSERT INTO statement_health (
                tenant_id, statement_id, reconciles, reconciliation_delta_paise,
                balance_continuous, first_divergent_row, first_divergent_page,
                pages_continuous, declared_transaction_count,
                extracted_transaction_count, ocr_page_count, total_page_count,
                checks
            ) VALUES (
                :tenant_id, :statement_id, :reconciles, :delta_paise,
                :balance_continuous, :divergent_row, :divergent_page,
                :pages_continuous, :declared, :extracted, :ocr_pages, :total_pages,
                CAST(:checks AS jsonb)
            )
            ON CONFLICT (statement_id) DO UPDATE
            SET reconciles = EXCLUDED.reconciles,
                reconciliation_delta_paise = EXCLUDED.reconciliation_delta_paise,
                balance_continuous = EXCLUDED.balance_continuous,
                first_divergent_row = EXCLUDED.first_divergent_row,
                first_divergent_page = EXCLUDED.first_divergent_page,
                pages_continuous = EXCLUDED.pages_continuous,
                declared_transaction_count = EXCLUDED.declared_transaction_count,
                extracted_transaction_count = EXCLUDED.extracted_transaction_count,
                ocr_page_count = EXCLUDED.ocr_page_count,
                total_page_count = EXCLUDED.total_page_count,
                checks = EXCLUDED.checks
            """
        ),
        {
            "tenant_id": tenant_id,
            "statement_id": statement_id,
            "reconciles": report.reconciles,
            # Paise as an exact integer, so "is it zero?" is an integer
            # comparison rather than a float epsilon argument.
            "delta_paise": int(report.delta * 100) if report.delta is not None else None,
            "balance_continuous": report.balance_continuous,
            "divergent_row": report.first_divergent_row,
            "divergent_page": report.first_divergent_page,
            "pages_continuous": report.pages_continuous,
            "declared": declared,
            "extracted": extracted,
            "ocr_pages": ocr_pages,
            "total_pages": total_pages,
            "checks": json.dumps(checks),
        },
    )


async def remove_statement_transactions(
    session: AsyncSession, *, statement_id: uuid.UUID
) -> int:
    """Delete a statement's ledger rows. Used by reprocess and by delete."""
    result = await session.execute(
        text("DELETE FROM transactions WHERE statement_id = :id RETURNING id"),
        {"id": statement_id},
    )
    return len(result.all())
