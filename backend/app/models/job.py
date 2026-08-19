"""Background processing jobs and their stage transitions."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantScopedMixin, TimestampMixin, enum_check, uuid_pk
from app.models.enums import JobState


class ProcessingJob(Base, TenantScopedMixin, TimestampMixin):
    """One statement's journey through the pipeline."""

    __tablename__ = "processing_jobs"

    id: Mapped[uuid.UUID] = uuid_pk()
    statement_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("statements.id", ondelete="CASCADE")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    celery_task_id: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32), default=JobState.QUEUED, nullable=False)

    # 0-100, for the upload progress bar. Derived from the stage, not guessed.
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)

    # Machine-readable only. A raw exception message could carry an amount or a
    # description, so it never lands in a column that a client can read.
    error_code: Mapped[str | None] = mapped_column(String(64))

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Final counts: extracted, verified, flagged, review_required, duplicates.
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (
        enum_check("state", JobState),
        Index("ix_processing_jobs_tenant_state", "tenant_id", "state"),
        Index("ix_processing_jobs_tenant_created", "tenant_id", "created_at"),
        Index("ix_processing_jobs_statement", "statement_id"),
    )


class JobEvent(Base, TenantScopedMixin):
    """A single stage transition, streamed to the UI over SSE.

    Append-only. This is what lets the upload screen show real stages rather
    than a spinner that lies about what is happening.
    """

    __tablename__ = "job_events"

    id: Mapped[uuid.UUID] = uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("processing_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )

    state: Mapped[str] = mapped_column(String(32), nullable=False)
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Presented to the user, so it must stay free of financial detail:
    # "Reading page 4 of 12", never "Found ₹45,231.50 to Swiggy".
    message: Mapped[str | None] = mapped_column(String(255))
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        enum_check("state", JobState),
        Index("ix_job_events_job_occurred", "job_id", "occurred_at"),
        Index("ix_job_events_tenant_occurred", "tenant_id", "occurred_at"),
    )
