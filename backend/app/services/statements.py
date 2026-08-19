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
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.core.logging import get_logger
from app.models.enums import AuditAction, DocumentType, StatementStatus, TrustStatus
from app.security.malware import scan as scan_for_malware
from app.security.pdf_validation import (
    PdfPasswordError,
    RejectionReason,
    unlock_pdf,
    validate_pdf,
)
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
    #: Stored and waiting for a password, which is neither accepted nor
    #: rejected. `statement_id` is populated so the caller can prompt for the
    #: password against this specific file rather than the whole batch.
    locked: bool = False


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

    # A locked statement is not a bad one. Every structural check that can be
    # run against an encrypted file has already run; the rest simply cannot be
    # judged until it opens. So it is stored and parked rather than discarded,
    # and the user unlocks the file they already sent instead of being told to
    # go and find it again.
    #
    # This also fixes a per-file problem the upload form cannot solve: one
    # password applies to the whole batch, and twelve statements from four
    # banks have four different passwords.
    locked = validation.reason in {
        RejectionReason.PASSWORD_REQUIRED,
        RejectionReason.WRONG_PASSWORD,
    }

    if not validation.ok and not locked:
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
    # The password is used exactly here and never persisted. What reaches object
    # storage is the decrypted PDF, so every later reprocess works without a
    # password and without this system keeping a secret to make that work.
    #
    # This is not a downgrade in protection. The object is encrypted at rest
    # under a per-tenant key derived from the master KEK, which is a great deal
    # stronger than a PDF password that most Indian banks generate from a
    # published formula over the holder's PAN and date of birth. Storing that
    # password would smuggle PAN-derived material into the database — the exact
    # class of identifier the rest of this system is built to keep out.
    payload = data
    if validation.ok and validation.is_encrypted and password:
        try:
            payload = unlock_pdf(data, password=password)
        except (PdfPasswordError, ValueError):
            # validate_pdf already opened this file with this password, so a
            # failure here is a rewrite problem, not a wrong password. Park it
            # rather than fail it: the unlock path retries cleanly.
            locked, payload = True, data

    # The hash is of the *original* bytes, always. Hashing the decrypted form
    # would let the same locked PDF be uploaded and unlocked repeatedly, each
    # pass looking new to the duplicate check.
    stored = storage.store_statement(
        tenant_id=tenant_id, statement_id=statement_id, data=payload
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
            "digest": digest,
            "document_type": str(DocumentType.UNKNOWN),
            "status": str(
                StatementStatus.PASSWORD_REQUIRED
                if locked
                else StatementStatus.UPLOADED
            ),
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

    # No job for a locked statement. There is nothing a worker could do with
    # bytes it cannot open, and queueing one would only produce a failure the
    # user would read as "your statement is broken" rather than "it needs a
    # password". The unlock endpoint creates the job once the file opens.
    job_id = (
        None
        if locked
        else await jobs.create_job(
            session, tenant_id=tenant_id, statement_id=statement_id, user_id=user_id
        )
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
        details={
            "count": validation.page_count,
            "statement_id": statement_id,
            **({"error_code": "password_required"} if locked else {}),
        },
    )

    if locked:
        logger.info(
            "statement_awaiting_password",
            tenant_id=str(tenant_id),
            user_id=str(user_id),
            statement_id=str(statement_id),
        )
        return UploadOutcome(
            filename=filename,
            accepted=False,
            locked=True,
            statement_id=statement_id,
            error_code=str(RejectionReason.PASSWORD_REQUIRED),
            message=(
                "That statement is password protected. It has been saved — "
                "enter its password to finish importing it."
            ),
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



#: How many wrong passwords a single statement will accept before it stops
#: answering. Low on purpose. Indian banks publish their statement password
#: formulas — HDFC uses the first four letters of the name plus DDMM of birth,
#: ICICI the first four letters plus DDMM, SBI a customer-set string — so the
#: search space for a targeted guess is small enough that a generous limit
#: turns this endpoint into a working oracle over PAN and date of birth.
MAX_UNLOCK_ATTEMPTS: Final = 5


@dataclass(frozen=True, slots=True)
class UnlockOutcome:
    unlocked: bool
    statement_id: uuid.UUID
    job_id: uuid.UUID | None = None
    page_count: int = 0
    attempts_remaining: int = 0
    error_code: str | None = None
    message: str | None = None


async def unlock_statement(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    statement_id: uuid.UUID,
    password: str,
    request_id: str | None = None,
    ip_address: str | None = None,
) -> UnlockOutcome:
    """Open a parked statement with its password and resume the pipeline.

    The password is a parameter and never becomes anything else. It is not
    logged, not written to the audit trail, not stored on the row, and not put
    on the Celery message — the worker is handed a decrypted object, so it
    never needs one. The audit record says an unlock was attempted and whether
    it worked; the secret itself lives only for the duration of this call.
    """

    row = (
        await session.execute(
            text(
                """
                SELECT status, storage_key, unlock_attempts
                FROM statements
                WHERE id = :id AND deleted_at IS NULL
                """
            ),
            {"id": statement_id},
        )
    ).mappings().one_or_none()

    # RLS scopes this session, so another tenant's statement is simply absent.
    # The 404 is the honest answer and it leaks nothing about what exists.
    if row is None:
        raise NotFoundError("That statement could not be found.")

    if row["status"] != StatementStatus.PASSWORD_REQUIRED:
        raise ConflictError("That statement is not waiting for a password.")

    attempts = int(row["unlock_attempts"])
    if attempts >= MAX_UNLOCK_ATTEMPTS:
        raise ValidationFailedError(
            "Too many incorrect passwords for this statement. Delete it and "
            "upload it again to try more.",
            details={"error_code": "unlock_attempts_exhausted"},
        )

    data = storage.load_statement(tenant_id=tenant_id, storage_key=row["storage_key"])

    try:
        plaintext = unlock_pdf(data, password=password)
    except (PdfPasswordError, ValueError):
        # Counted before anything else can fail, and committed even though the
        # request ends in an error response — a failed attempt that does not
        # persist is not a limit.
        attempts += 1
        await session.execute(
            text("UPDATE statements SET unlock_attempts = :n WHERE id = :id"),
            {"n": attempts, "id": statement_id},
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
            succeeded=False,
            details={"error_code": "wrong_password", "statement_id": statement_id},
        )
        logger.warning(
            "statement_unlock_failed",
            tenant_id=str(tenant_id),
            user_id=str(user_id),
            statement_id=str(statement_id),
            error_code="wrong_password",
            count=attempts,
        )
        return UnlockOutcome(
            unlocked=False,
            statement_id=statement_id,
            attempts_remaining=max(0, MAX_UNLOCK_ATTEMPTS - attempts),
            error_code="wrong_password",
            message="That password did not open the statement.",
        )

    # It opened. Re-run the full structural scan on the plaintext: everything
    # past "does it parse" — active content, embedded files, page count, the
    # decompression-bomb check — was unreachable while the file was encrypted,
    # so this is the first time those checks can actually see the document.
    validation = validate_pdf(plaintext)
    if not validation.ok:
        assert validation.reason is not None
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
        await audit.record(
            session,
            tenant_id=tenant_id,
            user_id=user_id,
            action=AuditAction.STATEMENT_UPLOAD,
            resource_type="statement",
            resource_id=statement_id,
            request_id=request_id,
            ip_address=ip_address,
            succeeded=False,
            details={"error_code": str(validation.reason), "statement_id": statement_id},
        )
        return UnlockOutcome(
            unlocked=False,
            statement_id=statement_id,
            error_code=str(validation.reason),
            message=validation.message,
        )

    # Same statement id, so the same deterministic key: this overwrites the
    # locked object rather than leaving a second copy behind.
    stored = storage.store_statement(
        tenant_id=tenant_id, statement_id=statement_id, data=plaintext
    )

    # file_sha256 is deliberately left alone. It is the fingerprint of what the
    # user uploaded, and it is what stops the same locked PDF being uploaded a
    # second time; rewriting it to the decrypted hash would quietly reopen that
    # door.
    await session.execute(
        text(
            """
            UPDATE statements
               SET status = :status,
                   storage_key = :key,
                   file_size_bytes = :size,
                   page_count = :pages,
                   unlock_attempts = 0,
                   error_code = NULL
             WHERE id = :id
            """
        ),
        {
            "status": str(StatementStatus.UPLOADED),
            "key": stored.key,
            "size": stored.size_bytes,
            "pages": validation.page_count,
            "id": statement_id,
        },
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
        "statement_unlocked",
        tenant_id=str(tenant_id),
        user_id=str(user_id),
        statement_id=str(statement_id),
        job_id=str(job_id),
        page_count=validation.page_count,
    )

    return UnlockOutcome(
        unlocked=True,
        statement_id=statement_id,
        job_id=job_id,
        page_count=validation.page_count,
        attempts_remaining=MAX_UNLOCK_ATTEMPTS,
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
