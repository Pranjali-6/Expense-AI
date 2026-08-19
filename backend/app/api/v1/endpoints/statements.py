"""Statement upload, listing and health."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

import jwt
from fastapi import APIRouter, BackgroundTasks, File, Form, Request, UploadFile, status
from fastapi.responses import StreamingResponse

from app.core.config import settings
from app.core.deps import CurrentUser, TenantSession, client_ip, parse_uuid
from app.core.errors import (
    AuthenticationError,
    NotFoundError,
    RateLimitError,
    UploadRejectedError,
)
from app.core.logging import get_logger
from app.core.rate_limit import RateLimitScope, check_rate_limit
from app.schemas.statement import (
    DownloadUrlResponse,
    StatementDetail,
    StatementHealthResponse,
    StatementSummary,
    UploadFileResult,
    UploadResponse,
)
from app.services import statements as statement_service
from app.services import storage

logger = get_logger(__name__)

router = APIRouter(prefix="/statements", tags=["statements"])

DOWNLOAD_TOKEN_TTL_SECONDS = 300


def _queue_ingestion(queued: list[tuple[uuid.UUID, uuid.UUID]], tenant_id: str) -> None:
    """Publish ingestion jobs. Synchronous, so FastAPI runs it in a threadpool
    and kombu's blocking publish never touches the event loop."""
    from workers.celery_app import celery_app

    for job_id, statement_id in queued:
        celery_app.send_task(
            "workers.tasks.ingest.process_statement",
            kwargs={
                "job_id": str(job_id),
                "tenant_id": tenant_id,
                "statement_id": str(statement_id),
            },
            queue="extract",
        )


@router.post(
    "/upload",
    response_model=UploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload one or more statement PDFs",
)
async def upload(
    request: Request,
    background: BackgroundTasks,
    session: TenantSession,
    current_user: CurrentUser,
    files: Annotated[list[UploadFile], File(description="Statement PDFs")],
    password: Annotated[str | None, Form()] = None,
) -> UploadResponse:
    """Accept a batch of PDFs and queue each for processing.

    Returns 202: the files are stored and queued, not parsed. Parsing a
    forty-page scanned statement takes long enough that doing it in the request
    would time out and hold a worker hostage.
    """
    if not files:
        raise UploadRejectedError("No files were uploaded.")

    if len(files) > settings.MAX_UPLOAD_FILES:
        raise UploadRejectedError(
            f"Please upload at most {settings.MAX_UPLOAD_FILES} files at a time.",
            details={"limit": settings.MAX_UPLOAD_FILES},
        )

    limit = await check_rate_limit(
        RateLimitScope.UPLOAD, str(current_user.id), cost=len(files)
    )
    if not limit.allowed:
        raise RateLimitError(
            "You have uploaded a lot of statements recently. Please wait a little.",
            details={"retry_after_seconds": limit.retry_after_seconds},
        )

    results: list[UploadFileResult] = []
    queued: list[tuple[uuid.UUID, uuid.UUID]] = []

    for upload_file in files:
        data = await upload_file.read()
        # The filename is used for the response only — never stored, never
        # logged. It routinely contains a name, an account number or a period.
        display_name = (upload_file.filename or "statement.pdf")[:120]

        outcome = await statement_service.ingest_upload(
            session,
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
            filename=display_name,
            data=data,
            password=password,
            request_id=getattr(request.state, "request_id", None),
            ip_address=client_ip(request),
        )

        results.append(
            UploadFileResult(
                filename=outcome.filename,
                accepted=outcome.accepted,
                statement_id=outcome.statement_id,
                job_id=outcome.job_id,
                page_count=outcome.page_count,
                error_code=outcome.error_code,
                message=outcome.message,
            )
        )
        if outcome.accepted and outcome.job_id and outcome.statement_id:
            queued.append((outcome.job_id, outcome.statement_id))

    # Queued as a background task rather than inline.
    #
    # The session dependency commits when it is finalised, which FastAPI does
    # before background tasks run. Dispatching inline would publish a job whose
    # statement row is still uncommitted, and a fast worker would pick it up and
    # find nothing — a race that only appears under load.
    if queued:
        background.add_task(
            _queue_ingestion, queued, str(current_user.tenant_id)
        )

    accepted = sum(1 for result in results if result.accepted)
    return UploadResponse(
        accepted=accepted, rejected=len(results) - accepted, results=results
    )


@router.get("", response_model=list[StatementSummary], summary="List statements")
async def list_statements(
    session: TenantSession, limit: int = 50, offset: int = 0
) -> list[StatementSummary]:
    rows = await statement_service.list_statements(
        session, limit=min(limit, 200), offset=offset
    )
    return [StatementSummary(**row) for row in rows]


@router.get("/{statement_id}", response_model=StatementDetail, summary="One statement")
async def get_statement(statement_id: str, session: TenantSession) -> StatementDetail:
    row = await statement_service.get_statement(session, parse_uuid(statement_id))
    return StatementDetail(
        **{
            **row,
            "opening_balance": str(row["opening_balance"]) if row.get("opening_balance") is not None else None,
            "closing_balance": str(row["closing_balance"]) if row.get("closing_balance") is not None else None,
            "job_id": None,
            "job_state": None,
            "progress": None,
        }
    )


@router.get(
    "/{statement_id}/health",
    response_model=StatementHealthResponse,
    summary="Whether this import can be trusted",
)
async def get_health(statement_id: str, session: TenantSession) -> StatementHealthResponse:
    target = parse_uuid(statement_id)
    await statement_service.get_statement(session, target)  # 404s across tenants
    health = await statement_service.get_health(session, target)
    if health is None:
        raise NotFoundError("No health report yet. Processing may still be running.")
    return StatementHealthResponse(**health)


@router.get(
    "/{statement_id}/download-url",
    response_model=DownloadUrlResponse,
    summary="Get a short-lived link to the original PDF",
)
async def download_url(
    statement_id: str, session: TenantSession, current_user: CurrentUser
) -> DownloadUrlResponse:
    target = parse_uuid(statement_id)
    await statement_service.get_statement(session, target)

    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "sub": str(current_user.id),
            "tid": str(current_user.tenant_id),
            # Bound to one statement: a leaked link exposes that document and
            # nothing else.
            "sid": str(target),
            "typ": "download",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=DOWNLOAD_TOKEN_TTL_SECONDS)).timestamp()),
        },
        settings.SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )

    return DownloadUrlResponse(
        url=f"{settings.API_V1_PREFIX}/statements/{target}/download?token={token}",
        expires_in=DOWNLOAD_TOKEN_TTL_SECONDS,
    )


@router.get("/{statement_id}/download", include_in_schema=False)
async def download(statement_id: str, token: str, session: TenantSession):
    """Stream the decrypted PDF.

    Takes its own token rather than the Authorization header so the link can be
    opened directly by a browser or an <embed>, where headers cannot be set.
    """
    try:
        claims = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": ["exp", "sub"]},
        )
    except jwt.PyJWTError:
        raise AuthenticationError("That link has expired.", error_code="invalid_token") from None

    if claims.get("typ") != "download" or claims.get("sid") != statement_id:
        raise AuthenticationError("That link is not valid.", error_code="invalid_token")

    target = parse_uuid(statement_id)
    row = await statement_service.get_statement(session, target)

    data = storage.load_statement(
        tenant_id=uuid.UUID(claims["tid"]), storage_key=row["storage_key"]
    )

    return StreamingResponse(
        iter([data]),
        media_type="application/pdf",
        headers={
            # `inline` so it renders in the detail panel rather than downloading.
            # The filename is synthesised from the id — the original is never
            # stored, and would often contain an account number.
            "Content-Disposition": f'inline; filename="statement-{target.hex[:8]}.pdf"',
            "Cache-Control": "no-store, private",
        },
    )


@router.delete("/{statement_id}", summary="Delete a statement and its transactions")
async def delete_statement(
    statement_id: str, request: Request, session: TenantSession, current_user: CurrentUser
) -> dict:
    target = parse_uuid(statement_id)
    removed = await statement_service.soft_delete(
        session,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        statement_id=target,
        request_id=getattr(request.state, "request_id", None),
    )
    return {
        "message": "Statement deleted.",
        "transactions_removed": removed,
    }
