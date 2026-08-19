"""Statement ingestion.

The upload path, from bytes on the wire to a queued job. Deliberately does no
parsing: it validates, stores, records and hands off. Extraction belongs to the
worker, on its own queue, where a malformed PDF can burn CPU without holding a
request open.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.models.enums import AuditAction, DocumentType, StatementStatus, TrustStatus
from app.security.malware import scan as scan_for_malware
from app.security.pdf_validation import validate_pdf
from app.services import audit, jobs, storage

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class UploadOutcome:
    """One file's result. A batch reports these per file rather than failing
    wholesale — one bad PDF in a drop of twelve should not discard the other
    eleven."""

    filename: str
    accepted: bool
    statement_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    error_code: str | None = None
    message: str | None = None
    page_count: int = 0


async def ingest_upload(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    filename: str,
    data: bytes,
    password: str | None = None,
    request_id: str | None = None,
    ip_address: str | None = None,
) -> UploadOutcome:
    """Validate, store and queue one statement."""

    # --- structural validation, before anything is written ----------------
    validation = validate_pdf(data, password=password)
    if not validation.ok:
        assert validation.reason is not None
        await audit.record(
            session,
            tenant_id=tenant_id,
            user_id=user_id,
            action=AuditAction.STATEMENT_UPLOAD,
            request_id=request_id,
            ip_address=ip_address,
            succeeded=False,
            details={"error_code": str(validation.reason), "count": len(validation.findings)},
        )
        logger.warning(
            "upload_rejected",
            tenant_id=str(tenant_id),
            user_id=str(user_id),
            error_code=str(validation.reason),
        )
        return UploadOutcome(
            filename=filename,
            accepted=False,
            error_code=str(validation.reason),
            message=validation.message,
        )

    # --- optional signature scan ------------------------------------------
    malware = scan_for_malware(data)
    if not malware.clean:
        await audit.record(
            session,
            tenant_id=tenant_id,
            user_id=user_id,
            action=AuditAction.STATEMENT_UPLOAD,
            request_id=request_id,
            succeeded=False,
            details={"error_code": "malware", "reason": malware.signature},
        )
        return UploadOutcome(
            filename=filename,
            accepted=False,
            error_code="malware",
            message="That file was rejected by the malware scanner.",
        )

    # --- content-hash duplicate check --------------------------------------
    # The same PDF uploaded twice is the most common source of duplicate
    # transactions, and catching it here costs one index lookup instead of a
    # full extraction that then has to be thrown away.
    statement_id = uuid.uuid4()
    import hashlib

    digest = hashlib.sha256(data).hexdigest()

    existing = (
        await session.execute(
            text(
                "SELECT id FROM statements "
                "WHERE file_sha256 = :digest AND deleted_at IS NULL"
            ),
            {"digest": digest},
        )
    ).scalar_one_or_none()

    if existing is not None:
        return UploadOutcome(
            filename=filename,
            accepted=False,
            statement_id=existing,
            error_code="duplicate_file",
            message="You have already uploaded this statement.",
            page_count=validation.page_count,
        )

    # --- store -------------------------------------------------------------
    stored = storage.store_statement(
        tenant_id=tenant_id, statement_id=statement_id, data=data
    )

    # ON CONFLICT rather than trusting the SELECT above. Two uploads of the same
    # file racing each other both pass the duplicate check and one then violates
    # the unique index — which surfaced as a 500 on a request whose correct
    # answer is a polite "already uploaded". The database is the arbiter; the
    # earlier check is only an optimisation that avoids storing bytes twice.
    inserted = (
        await session.execute(
        text(
            """
            INSERT INTO statements (
                id, tenant_id, uploaded_by, storage_key, file_size_bytes,
                file_sha256, document_type, status, trust_status, page_count
            ) VALUES (
                :id, :tenant_id, :user_id, :storage_key, :size,
                :digest, :document_type, :status, :trust_status, :pages
            )
            ON CONFLICT DO NOTHING
            RETURNING id
            """
        ),
        {
            "id": statement_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "storage_key": stored.key,
            "size": stored.size_bytes,
            "digest": stored.sha256,
            "document_type": str(DocumentType.UNKNOWN),
            "status": str(StatementStatus.UPLOADED),
            # Never `trusted` on arrival. Trust is earned by reconciliation,
            # which has not run yet.
            "trust_status": str(TrustStatus.PENDING),
            "pages": validation.page_count,
        },
        )
    ).one_or_none()

    if inserted is None:
        # Lost the race. The stored object is now orphaned, so remove it rather
        # than leave an encrypted blob nothing references.
        storage.delete_statement(tenant_id=tenant_id, storage_key=stored.key)
        return UploadOutcome(
            filename=filename,
            accepted=False,
            error_code="duplicate_file",
            message="You have already uploaded this statement.",
            page_count=validation.page_count,
        )

    job_id = await jobs.create_job(
        session, tenant_id=tenant_id, statement_id=statement_id, user_id=user_id
    )

    await audit.record(
        session,
        tenant_id=tenant_id,
        user_id=user_id,
        action=AuditAction.STATEMENT_UPLOAD,
        resource_type="statement",
        resource_id=statement_id,
        request_id=request_id,
        ip_address=ip_address,
        details={"count": validation.page_count, "statement_id": statement_id},
    )

    logger.info(
        "statement_uploaded",
        tenant_id=str(tenant_id),
        user_id=str(user_id),
        statement_id=str(statement_id),
        job_id=str(job_id),
        page_count=validation.page_count,
    )

    return UploadOutcome(
        filename=filename,
        accepted=True,
        statement_id=statement_id,
        job_id=job_id,
        page_count=validation.page_count,
    )


async def list_statements(
    session: AsyncSession, *, limit: int = 50, offset: int = 0
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                """
                SELECT s.id, s.bank_code, s.document_type, s.status, s.trust_status,
                       s.period_start, s.period_end, s.page_count,
                       s.transaction_count, s.duplicate_count, s.account_last4,
                       s.file_size_bytes, s.created_at, s.processed_at,
                       s.error_code,
                       a.bank_name, a.account_type,
                       -- What the parser read, kept distinct from
                       -- `s.transaction_count`, which counts rows that reached
                       -- the ledger. Conflating the two would let the UI claim
                       -- transactions are stored when they have only been read.
                       -- COALESCE, not a nullable column: a statement whose
                       -- health record does not exist yet has read zero
                       -- transactions, and "zero" is the honest answer where
                       -- null would be an API contract violation.
                       COALESCE(h.extracted_transaction_count, 0)
                           AS extracted_transaction_count,
                       j.id AS job_id, j.state AS job_state, j.progress
                FROM statements s
                LEFT JOIN accounts a ON a.id = s.account_id
                LEFT JOIN statement_health h ON h.statement_id = s.id
                LEFT JOIN LATERAL (
                    SELECT id, state, progress FROM processing_jobs
                    WHERE statement_id = s.id
                    ORDER BY created_at DESC LIMIT 1
                ) j ON true
                WHERE s.deleted_at IS NULL
                ORDER BY s.created_at DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            {"limit": limit, "offset": offset},
        )
    ).all()

    return [dict(row._mapping) for row in rows]


async def get_statement(session: AsyncSession, statement_id: uuid.UUID) -> dict[str, Any]:
    row = (
        await session.execute(
            text(
                """
                SELECT s.*, a.bank_name, a.account_type
                FROM statements s
                LEFT JOIN accounts a ON a.id = s.account_id
                WHERE s.id = :id AND s.deleted_at IS NULL
                """
            ),
            {"id": statement_id},
        )
    ).one_or_none()

    if row is None:
        # Row Level Security means another tenant's statement is already
        # invisible; this is the same 404 either way, which is the point.
        raise NotFoundError("That statement does not exist.")

    return dict(row._mapping)


async def get_health(session: AsyncSession, statement_id: uuid.UUID) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text("SELECT * FROM statement_health WHERE statement_id = :id"),
            {"id": statement_id},
        )
    ).one_or_none()
    return dict(row._mapping) if row else None


async def soft_delete(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    statement_id: uuid.UUID,
    request_id: str | None = None,
) -> int:
    """Remove a statement and the transactions it produced.

    Deleting the statement without its transactions would leave orphaned rows
    that still count towards every total, which is worse than not deleting at
    all. The object is removed from storage too — a soft-deleted row should not
    keep an encrypted PDF alive indefinitely.
    """
    statement = await get_statement(session, statement_id)

    removed = (
        await session.execute(
            text("DELETE FROM transactions WHERE statement_id = :id"),
            {"id": statement_id},
        )
    ).rowcount or 0

    await session.execute(
        text(
            "UPDATE statements SET deleted_at = :now, status = :status WHERE id = :id"
        ),
        {
            "now": datetime.now(timezone.utc),
            "status": str(StatementStatus.PROCESSED),
            "id": statement_id,
        },
    )

    storage.delete_statement(storage_key=statement["storage_key"])

    await audit.record(
        session,
        tenant_id=tenant_id,
        user_id=user_id,
        action=AuditAction.STATEMENT_DELETE,
        resource_type="statement",
        resource_id=statement_id,
        request_id=request_id,
        details={"count": removed, "statement_id": statement_id},
    )

    logger.info(
        "statement_deleted",
        tenant_id=str(tenant_id),
        statement_id=str(statement_id),
        count=removed,
    )
    return removed
