"""Processing job state machine and progress events.

Every stage transition does two things: it writes a durable ``job_events`` row,
and it publishes the same event to a Redis channel. The durable row is the
record; the publish is the live feed.

Both exist because either alone is wrong. Publish-only loses everything if the
client is not connected at that instant — reload the page mid-import and the
progress bar has no idea what happened. Database-only forces the UI to poll,
which is either laggy or wasteful. Writing both means a client that connects
late replays history and then follows along.

Progress messages are shown to users, so they say *what stage is running* and
never what was found. "Reading page 4 of 12" is fine; "Found ₹45,231.50 to
Swiggy" is a financial value on a screen and, worse, in a log.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.redis_client import get_redis
from app.models.enums import JobState

logger = get_logger(__name__)

#: Nominal progress at the end of each stage. Derived from the pipeline's real
#: shape rather than a timer, so the bar reflects work done, not time passed.
STAGE_PROGRESS: dict[JobState, int] = {
    JobState.QUEUED: 0,
    JobState.PROCESSING: 10,
    JobState.EXTRACTING: 45,
    JobState.VALIDATING: 70,
    JobState.CATEGORIZING: 90,
    JobState.COMPLETED: 100,
    JobState.REVIEW_REQUIRED: 100,
    JobState.FAILED: 100,
}

TERMINAL_STATES = {JobState.COMPLETED, JobState.FAILED, JobState.REVIEW_REQUIRED}

#: Kept short so a browser that reconnects after a long gap still finds the
#: history it missed, without Redis holding job chatter indefinitely.
EVENT_CHANNEL_TTL_SECONDS = 3600


def channel_for(job_id: uuid.UUID | str) -> str:
    return f"jobs:{job_id}"


@dataclass(frozen=True, slots=True)
class JobEventPayload:
    job_id: str
    state: str
    stage: str
    progress: int
    message: str | None
    occurred_at: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "job_id": self.job_id,
                "state": self.state,
                "stage": self.stage,
                "progress": self.progress,
                "message": self.message,
                "occurred_at": self.occurred_at,
            }
        )


async def create_job(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    statement_id: uuid.UUID,
    user_id: uuid.UUID | None,
) -> uuid.UUID:
    job_id = (
        await session.execute(
            text(
                """
                INSERT INTO processing_jobs (
                    tenant_id, statement_id, created_by, state, progress
                ) VALUES (
                    :tenant_id, :statement_id, :user_id, :state, 0
                )
                RETURNING id
                """
            ),
            {
                "tenant_id": tenant_id,
                "statement_id": statement_id,
                "user_id": user_id,
                "state": str(JobState.QUEUED),
            },
        )
    ).scalar_one()

    await emit(
        session,
        tenant_id=tenant_id,
        job_id=job_id,
        state=JobState.QUEUED,
        stage="queued",
        message="Waiting for a worker",
    )
    return job_id


async def emit(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    job_id: uuid.UUID,
    state: JobState,
    stage: str,
    message: str | None = None,
    duration_ms: int | None = None,
    progress: int | None = None,
) -> None:
    """Record a stage transition and publish it."""
    now = datetime.now(timezone.utc)
    value = progress if progress is not None else STAGE_PROGRESS.get(state, 0)

    await session.execute(
        text(
            """
            INSERT INTO job_events (
                tenant_id, job_id, state, stage, progress, message,
                duration_ms, occurred_at
            ) VALUES (
                :tenant_id, :job_id, :state, :stage, :progress, :message,
                :duration_ms, :occurred_at
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "job_id": job_id,
            "state": str(state),
            "stage": stage,
            "progress": value,
            "message": (message or "")[:255] or None,
            "duration_ms": duration_ms,
            "occurred_at": now,
        },
    )

    await session.execute(
        text(
            "UPDATE processing_jobs SET state = :state, progress = :progress, "
            "started_at = COALESCE(started_at, :now) WHERE id = :job_id"
        ),
        {"state": str(state), "progress": value, "now": now, "job_id": job_id},
    )

    payload = JobEventPayload(
        job_id=str(job_id),
        state=str(state),
        stage=stage,
        progress=value,
        message=message,
        occurred_at=now.isoformat(),
    )

    # Publishing must never be able to fail a job. Redis being down should cost
    # a live progress bar, not a user's statement — the durable row above is
    # already written and the UI falls back to polling it.
    try:
        redis = get_redis()
        await redis.publish(channel_for(job_id), payload.to_json())
    except Exception:
        logger.warning(
            "job_event_publish_failed", job_id=str(job_id), error_code="redis_error"
        )

    logger.info(
        "job_stage",
        job_id=str(job_id),
        tenant_id=str(tenant_id),
        stage=stage,
        status=str(state),
        duration_ms=duration_ms,
    )


async def finish(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    job_id: uuid.UUID,
    state: JobState,
    summary: dict[str, Any] | None = None,
    error_code: str | None = None,
    message: str | None = None,
) -> None:
    await session.execute(
        text(
            """
            UPDATE processing_jobs
            SET state = :state, progress = 100, finished_at = :now,
                summary = CAST(:summary AS jsonb), error_code = :error_code
            WHERE id = :job_id
            """
        ),
        {
            "state": str(state),
            "now": datetime.now(timezone.utc),
            "summary": json.dumps(summary) if summary else None,
            "error_code": error_code,
            "job_id": job_id,
        },
    )
    await emit(
        session,
        tenant_id=tenant_id,
        job_id=job_id,
        state=state,
        stage="finished",
        message=message,
        progress=100,
    )


async def load_events(
    session: AsyncSession, *, job_id: uuid.UUID
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                """
                SELECT state, stage, progress, message, occurred_at
                FROM job_events WHERE job_id = :job_id
                ORDER BY occurred_at ASC
                """
            ),
            {"job_id": job_id},
        )
    ).all()

    return [
        {
            "job_id": str(job_id),
            "state": row.state,
            "stage": row.stage,
            "progress": row.progress,
            "message": row.message,
            "occurred_at": row.occurred_at.isoformat(),
        }
        for row in rows
    ]
