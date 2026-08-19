"""Statement ingestion pipeline.

Re-validate what was actually stored, read the pages, classify the document,
dispatch to a bank parser, then reconcile, deduplicate, score and persist.

``trust_status`` reaches ``trusted`` only on an exact ₹0.00 reconciliation. A
statement is never marked trusted by a stage that did not check its arithmetic,
and reconciliation runs on the parser's own reading rather than on the
generator's numbers — the question is whether *this* read closes.
"""

from __future__ import annotations

import time
import uuid

from app.core.logging import bind_context, clear_context, get_logger
from app.observability import metrics
from app.db.session import scoped_session
from app.extraction.classifier import classify_document
from app.extraction.pipeline import parse_extracted, read_document
from app.models.enums import JobState, StatementStatus, TrustStatus
from app.security.pdf_validation import validate_pdf
from app.services import categorization, jobs, ledger, notifications, storage
from sqlalchemy import text

from workers import runtime
from workers.celery_app import celery_app

logger = get_logger(__name__)

# ``classify_document`` moved to ``app.extraction.classifier`` in P4 so the
# accuracy harness and the worker share one implementation. Re-exported here
# because a second copy is how two answers to the same question appear.
__all__ = ["classify_document", "process_statement"]


# --------------------------------------------------------------------------- #
# The task
# --------------------------------------------------------------------------- #

async def _run(job_id: uuid.UUID, tenant_id: uuid.UUID, statement_id: uuid.UUID) -> None:
    started = time.perf_counter()

    async with scoped_session(tenant_id, actor="system") as session:
        await jobs.emit(
            session, tenant_id=tenant_id, job_id=job_id,
            state=JobState.PROCESSING, stage="validating",
            message="Checking the file",
        )
        row = (
            await session.execute(
                text("SELECT storage_key FROM statements WHERE id = :id"),
                {"id": statement_id},
            )
        ).one_or_none()

    if row is None:
        async with scoped_session(tenant_id, actor="system") as session:
            await jobs.finish(
                session, tenant_id=tenant_id, job_id=job_id,
                state=JobState.FAILED, error_code="statement_missing",
                message="The statement could not be found",
            )
        return

    data = storage.load_statement(tenant_id=tenant_id, storage_key=row.storage_key)

    # Re-validate what came *back* from storage rather than trusting the check
    # done at upload. The bytes that were validated and the bytes that were
    # stored are only the same thing if nothing went wrong in between.
    validation = validate_pdf(data)
    if not validation.ok:
        metrics.extraction_failures_total.labels(
            bank_code="unknown", stage="validation", error_code=str(validation.reason)
        ).inc()
        async with scoped_session(tenant_id, actor="system") as session:
            await notifications.for_statement(
                session, tenant_id=tenant_id, statement_id=statement_id,
                inserted=0, duplicates=0, review_required=0,
                reconciles=False, unverifiable=True,
                failed_error_code=str(validation.reason),
            )
            await session.execute(
                text(
                    "UPDATE statements SET status = :status, error_code = :code "
                    "WHERE id = :id"
                ),
                {
                    "status": str(StatementStatus.FAILED),
                    "code": str(validation.reason),
                    "id": statement_id,
                },
            )
            await jobs.finish(
                session, tenant_id=tenant_id, job_id=job_id,
                state=JobState.FAILED, error_code=str(validation.reason),
                message="The file failed validation",
            )
        return

    async with scoped_session(tenant_id, actor="system") as session:
        await jobs.emit(
            session, tenant_id=tenant_id, job_id=job_id,
            state=JobState.EXTRACTING, stage="reading_pages",
            message=f"Reading {validation.page_count} pages",
        )

    # Heavy work runs with no database session held. A page of OCR takes
    # seconds; a connection held across it is a connection nobody else can use.
    document = read_document(data)

    async with scoped_session(tenant_id, actor="system") as session:
        await jobs.emit(
            session, tenant_id=tenant_id, job_id=job_id,
            state=JobState.EXTRACTING, stage="parsing",
            message="Identifying the bank and reading transactions",
        )

    outcome = parse_extracted(document)
    result = outcome.result
    metadata = result.metadata

    total_pages = len(document.pages)
    ocr_pages = document.ocr_page_count
    rows_by_page: dict[int, int] = {}
    for transaction in result.transactions:
        if transaction.source_page:
            rows_by_page[transaction.source_page] = rows_by_page.get(transaction.source_page, 0) + 1

    async with scoped_session(tenant_id, actor="system") as session:
        for page in document.pages:
            await session.execute(
                text(
                    """
                    INSERT INTO statement_pages (
                        tenant_id, statement_id, page_number, extraction_method,
                        char_count, table_count, row_count, ocr_confidence
                    ) VALUES (
                        :tenant_id, :statement_id, :page_number, :method,
                        :char_count, :tables, :rows, :ocr_confidence
                    )
                    ON CONFLICT (statement_id, page_number) DO UPDATE
                    SET char_count = EXCLUDED.char_count,
                        extraction_method = EXCLUDED.extraction_method,
                        table_count = EXCLUDED.table_count,
                        row_count = EXCLUDED.row_count,
                        ocr_confidence = EXCLUDED.ocr_confidence
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "statement_id": statement_id,
                    "page_number": page.page_number,
                    "method": str(page.method),
                    "char_count": page.char_count,
                    "tables": len(page.tables),
                    "rows": rows_by_page.get(page.page_number, 0),
                    "ocr_confidence": page.ocr_confidence,
                },
            )

        await session.execute(
            text(
                """
                UPDATE statements
                SET document_type = :document_type,
                    bank_code = :bank_code,
                    bank_detection_confidence = :bank_confidence,
                    parser_name = :parser_name,
                    parser_version = :parser_version,
                    period_start = :period_start,
                    period_end = :period_end,
                    opening_balance = :opening_balance,
                    closing_balance = :closing_balance,
                    account_last4 = :account_last4,
                    page_count = :pages,
                    extraction_method = :method
                WHERE id = :id
                """
            ),
            {
                "document_type": str(outcome.document_type),
                "bank_code": metadata.bank_code,
                "bank_confidence": outcome.dispatch.confidence,
                "parser_name": result.parser_name,
                "parser_version": result.parser_version,
                "period_start": metadata.period_start,
                "period_end": metadata.period_end,
                "opening_balance": metadata.opening_balance,
                "closing_balance": metadata.closing_balance,
                "account_last4": metadata.account_last4,
                "pages": total_pages,
                "method": str(outcome.method),
                "id": statement_id,
            },
        )

        await jobs.emit(
            session, tenant_id=tenant_id, job_id=job_id,
            state=JobState.VALIDATING, stage="reconciling",
            message="Checking the statement adds up",
        )

    # --- the trust chain -----------------------------------------------------
    # Reconcile, fingerprint, score and write, all in one transaction. A
    # statement whose rows landed but whose trust verdict did not would be a
    # ledger nobody could interpret.
    async with scoped_session(tenant_id, actor="system") as session:
        outcome_result = await ledger.persist(
            session, tenant_id=tenant_id, statement_id=statement_id, outcome=outcome
        )

    # --- the categorisation cascade -----------------------------------------
    # Runs after the ledger write, in its own transaction, because it may make
    # network calls: an AI provider timing out must not roll back transactions
    # that were already read and reconciled correctly.
    #
    # The actor is `ai` for this session, so the database trigger protecting
    # verified rows applies to everything it does — the guard is enforced by the
    # row, not by this code remembering to check.
    async with scoped_session(tenant_id, actor="ai") as session:
        await jobs.emit(
            session, tenant_id=tenant_id, job_id=job_id,
            state=JobState.CATEGORIZING, stage="categorizing",
            message="Categorising transactions",
        )
        cascade = await categorization.run_cascade(
            session, tenant_id=tenant_id, statement_id=statement_id
        )

    # Refresh derived intelligence for this tenant. Dispatched rather than run
    # inline: subscriptions and anomalies are a whole-history computation, and
    # an import should not wait on it to report success.
    try:
        from workers.tasks.intelligence import refresh_tenant

        refresh_tenant.delay(tenant_id=str(tenant_id))
    except Exception as exc:
        # A queueing failure must not fail an import that already succeeded.
        # The nightly sweep will pick it up.
        logger.warning(
            "intelligence_refresh_not_queued",
            stage="intelligence", error_code=type(exc).__name__,
        )

    async with scoped_session(tenant_id, actor="system") as session:
        duration_ms = int((time.perf_counter() - started) * 1000)

        category_sources = {
            str(row.category_source): int(row.count)
            for row in (
                await session.execute(
                    text(
                        "SELECT category_source, count(*) AS count FROM transactions "
                        "WHERE statement_id = :id GROUP BY category_source"
                    ),
                    {"id": statement_id},
                )
            ).all()
        }
        _record_pipeline_metrics(
            bank_code=metadata.bank_code or "unknown",
            document_type=str(outcome.document_type),
            duration_seconds=time.perf_counter() - started,
            pages=document.pages,
            ocr_pages=ocr_pages,
            result=outcome_result,
            category_sources=category_sources,
        )
        # Re-read rather than trusting the pre-cascade count: categorisation
        # changes review status, and reporting the earlier number would tell the
        # user to review rows that were since settled.
        needs_review = bool(
            (
                await session.execute(
                    text(
                        "SELECT count(*) FROM transactions "
                        "WHERE statement_id = :id AND review_status = 'review_required'"
                    ),
                    {"id": statement_id},
                )
            ).scalar_one()
        )

        # One notification per import, chosen by what actually happened — see
        # services/notifications.py on why a clean import is not announced the
        # same way a statement that failed to reconcile is.
        await notifications.for_statement(
            session,
            tenant_id=tenant_id,
            statement_id=statement_id,
            inserted=outcome_result.inserted,
            duplicates=outcome_result.duplicates,
            review_required=outcome_result.review_required,
            reconciles=outcome_result.report.reconciles,
            unverifiable=outcome_result.report.unverifiable,
        )

        await jobs.finish(
            session,
            tenant_id=tenant_id,
            job_id=job_id,
            state=JobState.REVIEW_REQUIRED if needs_review else JobState.COMPLETED,
            summary={
                "pages": total_pages,
                "ocr_pages": ocr_pages,
                "extraction_method": str(outcome.method),
                "document_type": str(outcome.document_type),
                "bank_code": metadata.bank_code or "unknown",
                "parser": result.parser_name,
                "transactions_extracted": len(result.transactions),
                "transactions_added": outcome_result.inserted,
                "duplicates_skipped": outcome_result.duplicates,
                "suspected_duplicates": outcome_result.suspected_duplicates,
                "auto_approved": outcome_result.auto_approved,
                "flagged": outcome_result.flagged,
                "review_required": outcome_result.review_required,
                "transfers_paired": outcome_result.transfers_paired,
                "trust_status": str(outcome_result.trust_status),
                "reconciles": outcome_result.report.reconciles,
                "categorised_by_user_rule": cascade.by_user_rule,
                "categorised_by_history": cascade.by_history,
                "categorised_by_ai": cascade.by_ai,
                "ai_payloads_blocked": cascade.ai_blocked,
                "ai_injections_quarantined": cascade.ai_quarantined,
                "ai_cost_inr": str(cascade.cost_inr),
                "left_uncategorised": cascade.left_uncategorised,
                "duration_ms": duration_ms,
            },
            message=_summary_message(outcome_result),
        )


def _record_pipeline_metrics(
    *,
    bank_code: str,
    document_type: str,
    duration_seconds: float,
    pages,
    ocr_pages: int,
    result: "ledger.LedgerResult",
    category_sources: dict[str, int],
) -> None:
    """Everything Prometheus learns from one import, in one place.

    Gathered here rather than scattered through the pipeline for a specific
    reason: every label on every metric below is a *shape* — a bank code, a
    stage, a status, a count. Keeping the increments together makes that
    checkable at a glance. A merchant or an amount in a label would turn a
    metrics endpoint, which is unauthenticated by convention, into a data leak
    with a fifteen-second scrape interval.
    """
    metrics.extraction_duration_seconds.labels(
        bank_code=bank_code, document_type=document_type
    ).observe(duration_seconds)

    if ocr_pages:
        metrics.ocr_pages_total.labels(bank_code=bank_code).inc(ocr_pages)
    for page in pages:
        metrics.pages_processed_total.labels(
            bank_code=bank_code, method=str(getattr(page, "method", "unknown"))
        ).inc()

    report = result.report
    if report.delta is not None:
        # Observed in paise so the histogram buckets are integers: "zero, one
        # paisa, one rupee, a hundred" is a scale you can reason about, and
        # zero is the only bucket that means the statement passed.
        metrics.reconciliation_delta_paise.observe(int(abs(report.delta) * 100))
    if not report.reconciles:
        metrics.validation_failures_total.labels(
            bank_code=bank_code, check="reconciliation"
        ).inc()

    if result.duplicates:
        metrics.duplicates_detected_total.labels(method="fingerprint").inc(
            result.duplicates
        )
    if result.suspected_duplicates:
        metrics.duplicates_detected_total.labels(method="fuzzy").inc(
            result.suspected_duplicates
        )

    for status, count in (
        ("auto_approved", result.auto_approved),
        ("flagged", result.flagged),
        ("review_required", result.review_required),
    ):
        if count:
            metrics.transactions_ingested_total.labels(
                bank_code=bank_code, review_status=status
            ).inc(count)

    # Read from the rows themselves rather than from the cascade's tallies.
    # The cascade only sees what the deterministic tiers could not settle, so
    # its counters describe the hard cases; the column describes every
    # transaction, including the ones a parser rule categorised at read time.
    for source, count in category_sources.items():
        if count:
            metrics.categorization_total.labels(category_source=source).inc(count)


def _summary_message(result: "ledger.LedgerResult") -> str:
    """One honest sentence. Never claims more than reconciliation established."""
    if result.inserted == 0 and result.duplicates:
        return f"Already imported — {result.duplicates} duplicate rows skipped"
    if result.trust_status == TrustStatus.TRUSTED:
        return f"{result.inserted} transactions added · reconciles exactly"
    if result.trust_status == TrustStatus.UNTRUSTED:
        return f"{result.inserted} transactions added · does not reconcile"
    return f"{result.inserted} transactions added · arithmetic could not be checked"


@celery_app.task(
    name="workers.tasks.ingest.process_statement",
    bind=True,
    max_retries=2,
    default_retry_delay=15,
    queue="extract",
)
def process_statement(self, *, job_id: str, tenant_id: str, statement_id: str) -> dict:
    """Entry point. Runs on the heavy queue; PDF work never blocks the API."""
    clear_context()
    bind_context(job_id=job_id, tenant_id=tenant_id)

    try:
        runtime.run(_run(uuid.UUID(job_id), uuid.UUID(tenant_id), uuid.UUID(statement_id)))
        return {"job_id": job_id, "status": "completed"}
    except Exception as exc:
        logger.error(
            "ingest_task_failed",
            job_id=job_id,
            error_code=type(exc).__name__,
            exc_info=exc,
        )

        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc)

        # Bind the code, not the exception. Python unbinds `exc` at the end of
        # the except block, so a closure over it is one refactor away from a
        # NameError — and holding the exception object alive would keep its
        # traceback, and any statement content in its message, reachable from a
        # code path whose whole job is to write an error *code* and nothing else.
        error_code = type(exc).__name__

        async def _mark_failed() -> None:
            async with scoped_session(uuid.UUID(tenant_id), actor="system") as session:
                await session.execute(
                    text(
                        "UPDATE statements SET status = :status, error_code = :code "
                        "WHERE id = :id"
                    ),
                    {
                        "status": str(StatementStatus.FAILED),
                        "code": error_code,
                        "id": uuid.UUID(statement_id),
                    },
                )
                await jobs.finish(
                    session,
                    tenant_id=uuid.UUID(tenant_id),
                    job_id=uuid.UUID(job_id),
                    state=JobState.FAILED,
                    error_code=error_code,
                    message="Processing failed",
                )

        try:
            runtime.run(_mark_failed())
        except Exception:
            logger.error("ingest_failure_record_failed", job_id=job_id,
                         error_code="bookkeeping")
        raise
    finally:
        # The engine and redis client are closed inside the task's own loop by
        # `runtime.run`; asyncpg binds connections to the loop that opened them,
        # and closing them from a second loop logs an error for work that
        # actually succeeded.
        clear_context()
