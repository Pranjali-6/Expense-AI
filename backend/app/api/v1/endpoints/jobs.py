"""Processing job status and the live progress stream."""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import text

from app.core.deps import CurrentUser, TenantSession, parse_uuid
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.core.redis_client import get_redis
from app.db.session import scoped_session
from app.models.enums import JobState
from app.schemas.statement import JobResponse
from app.services import jobs as job_service

logger = get_logger(__name__)

router = APIRouter(prefix="/jobs", tags=["jobs"])

#: Sent when nothing has happened for a while. Without it an idle connection is
#: silently dropped by proxies and the browser never notices it stopped
#: receiving updates.
HEARTBEAT_SECONDS = 20
STREAM_TIMEOUT_SECONDS = 900


@router.get("/{job_id}", response_model=JobResponse, summary="Job status")
async def get_job(job_id: str, session: TenantSession) -> JobResponse:
    row = (
        await session.execute(
            text(
                "SELECT id, statement_id, state, progress, attempt, error_code, "
                "       started_at, finished_at, summary "
                "FROM processing_jobs WHERE id = :id"
            ),
            {"id": parse_uuid(job_id)},
        )
    ).one_or_none()

    if row is None:
        raise NotFoundError("That job does not exist.")
    return JobResponse(**dict(row._mapping))


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


@router.get("/{job_id}/events", summary="Live progress stream (SSE)")
async def stream_events(job_id: str, request: Request, current_user: CurrentUser):
    """Server-Sent Events for one job.

    History is replayed before the live feed begins, so a client that connects
    late — or reloads the page mid-import — sees the stages it missed instead of
    an empty progress bar. Streaming without that replay looks fine in
    development, where you always happen to be watching.

    SSE rather than WebSockets: progress is one-directional, and SSE survives a
    plain HTTP proxy, reconnects on its own, and needs no protocol upgrade.
    """
    target = parse_uuid(job_id)

    # Authorisation happens once, up front, on a scoped session. RLS means a
    # job belonging to another tenant simply is not found.
    async with scoped_session(current_user.tenant_id) as session:
        exists = (
            await session.execute(
                text("SELECT state FROM processing_jobs WHERE id = :id"),
                {"id": target},
            )
        ).scalar_one_or_none()
        if exists is None:
            raise NotFoundError("That job does not exist.")

    async def event_source() -> AsyncIterator[str]:
        redis = get_redis()
        pubsub = redis.pubsub()

        # Subscribe *before* reading history.
        #
        # The obvious order — read history, then subscribe — drops any event
        # published in the gap between the two. It looks correct in testing
        # because the gap is milliseconds and jobs usually take longer; it shows
        # up as a stage silently missing from a fast import, which is exactly
        # the kind of bug that gets dismissed as a rendering glitch. Subscribing
        # first makes the window overlap instead, and duplicates are filtered
        # below by timestamp.
        await pubsub.subscribe(job_service.channel_for(target))

        try:
            async with scoped_session(current_user.tenant_id) as session:
                history = await job_service.load_events(session, job_id=target)

            last_seen = ""
            for event in history:
                last_seen = max(last_seen, event["occurred_at"])
                yield _sse(event)

            terminal = {
                str(JobState.COMPLETED),
                str(JobState.FAILED),
                str(JobState.REVIEW_REQUIRED),
            }

            # Already finished — say so and close rather than holding a
            # connection open for a job that will never emit again.
            if history and history[-1]["state"] in terminal:
                yield _sse({"type": "done"})
                return

            loop = asyncio.get_running_loop()
            deadline = loop.time() + STREAM_TIMEOUT_SECONDS

            while loop.time() < deadline:
                if await request.is_disconnected():
                    break

                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=HEARTBEAT_SECONDS
                )

                if message is None:
                    # A comment frame: stops proxies reaping an idle
                    # connection, and EventSource ignores it.
                    yield ": keepalive\n\n"
                    continue

                payload = message.get("data")
                if not payload:
                    continue

                try:
                    parsed = json.loads(payload)
                except json.JSONDecodeError:
                    continue

                # Skip anything the history replay already covered.
                occurred_at = parsed.get("occurred_at", "")
                if occurred_at and occurred_at <= last_seen:
                    continue
                last_seen = max(last_seen, occurred_at)

                yield f"data: {payload}\n\n"

                if parsed.get("state") in terminal:
                    yield _sse({"type": "done"})
                    break
        finally:
            try:
                await pubsub.unsubscribe(job_service.channel_for(target))
                await pubsub.aclose()
            except Exception:
                pass

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store, no-transform",
            # nginx buffers proxied responses by default, which would hold every
            # event until the stream closed and defeat the entire feature.
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
