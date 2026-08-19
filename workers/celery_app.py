"""Celery application.

Two queues, deliberately separated:

*   ``extract``      CPU-bound PDF work — PyMuPDF, pdfplumber, Camelot, OCR.
                     Low concurrency, recycled workers (the PDF stack leaks
                     memory across large documents), scaled independently.
*   ``default``      Everything light — categorization, notifications, exports.
*   ``intelligence`` Scheduled analytics recomputation.

Task modules are registered phase by phase; P0 ships the app itself so the
worker containers start, answer ``celery inspect ping``, and report healthy.
"""

from __future__ import annotations

import os
import time

from celery import Celery
from celery.schedules import crontab
from celery.signals import (
    celeryd_init,
    setup_logging,
    task_postrun,
    task_prerun,
    worker_process_shutdown,
)

from app.core.config import settings
from app.core.logging import bind_context, clear_context, configure_logging, get_logger
from app.observability import exporter, metrics

logger = get_logger(__name__)

#: Task start times, keyed by task id, for the stage-duration histogram.
#: Bounded in practice by the number of tasks in flight on one worker.
_STARTED: dict[str, float] = {}

celery_app = Celery(
    "expense_ai",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

celery_app.conf.update(
    # --- serialization ------------------------------------------------------
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Kolkata",
    enable_utc=True,
    # --- routing ------------------------------------------------------------
    task_default_queue="default",
    task_routes={
        "workers.tasks.extract.*": {"queue": "extract"},
        "workers.tasks.ingest.*": {"queue": "extract"},
        "workers.tasks.intelligence.*": {"queue": "intelligence"},
        "workers.tasks.narrative.*": {"queue": "default"},
        "workers.tasks.maintenance.*": {"queue": "default"},
    },
    # --- reliability --------------------------------------------------------
    # Acknowledge only after the task completes. A worker killed mid-extraction
    # must not silently lose a user's statement.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    task_time_limit=900,
    task_soft_time_limit=840,
    result_expires=86_400,
    broker_connection_retry_on_startup=True,
    # --- results ------------------------------------------------------------
    # Task results are progress metadata only. Financial data lives in
    # PostgreSQL; nothing of substance is reconstructible from the backend.
    result_extended=True,
    # --- scheduled work -----------------------------------------------------
    # Derived intelligence is rebuilt nightly rather than under request, so a
    # dashboard is fast and two people looking at the same month see the same
    # numbers regardless of when they asked.
    #
    # 03:30 IST: after midnight statements have settled, before anyone is
    # looking. Times are IST because the timezone above is Asia/Kolkata and the
    # users are Indian; a UTC schedule would run mid-evening for them.
    # 04:00 phrases what 03:30 computed. Two entries rather than one task with
    # two steps, so the deterministic half runs to completion whatever the
    # model does — and so "no model is invoked in the intelligence refresh"
    # stays a fact about the code rather than a claim about its configuration.
    beat_schedule={
        "nightly-intelligence-refresh": {
            "task": "workers.tasks.intelligence.nightly_refresh",
            "schedule": crontab(hour=3, minute=30),
        },
        "nightly-narratives": {
            "task": "workers.tasks.narrative.nightly_narratives",
            "schedule": crontab(hour=4, minute=0),
        },
        # Housekeeping last, at the quietest hour: retention removes rows, and
        # the orphan purge removes the objects those rows used to point at, so
        # the order between them is not arbitrary.
        "retention-sweep": {
            "task": "workers.tasks.maintenance.retention_sweep",
            "schedule": crontab(hour=4, minute=30),
        },
        # Reports, never deletes. Retention already removes the objects it
        # orphans; anything this finds is a discrepancy worth understanding
        # before it is worth cleaning up.
        "reconcile-objects": {
            "task": "workers.tasks.maintenance.reconcile_objects",
            "schedule": crontab(hour=5, minute=0),
        },
    },
)

# Task modules register themselves in workers/tasks/__init__.py, which keeps
# the set of live tasks explicit and greppable rather than import-order magic.
celery_app.autodiscover_tasks(["workers"], related_name="tasks", force=True)


@setup_logging.connect
def _configure_worker_logging(**_kwargs) -> None:
    """Replace Celery's logging with ours.

    Celery's default handlers would emit task arguments on failure — and task
    arguments carry statement ids, tenant ids and, if we are ever careless,
    worse. Routing through our pipeline guarantees the allow-list applies.
    """
    configure_logging()


@task_prerun.connect
def _bind_task_context(task_id=None, task=None, kwargs=None, **_kwargs) -> None:
    clear_context()
    payload = kwargs or {}
    bind_context(
        job_id=payload.get("job_id"),
        tenant_id=payload.get("tenant_id"),
        user_id=payload.get("user_id"),
    )
    logger.info("task_started", task_name=getattr(task, "name", None), component="worker")


@task_postrun.connect
def _clear_task_context(task=None, state=None, **_kwargs) -> None:
    logger.info(
        "task_finished",
        task_name=getattr(task, "name", None),
        status=state,
        component="worker",
    )
    clear_context()


@celeryd_init.connect
def _start_metrics_server(**_kwargs) -> None:
    """Expose ``/metrics`` from the worker's main process.

    Fires once, in the parent, before the pool forks — which is exactly where
    the multiprocess sample directory must be prepared. Children inherit the
    environment variable and write their samples into it; this server reads the
    whole directory, so a counter incremented in a forked child is visible to a
    scrape of the parent. Without that indirection, every worker metric in this
    codebase would export as zero forever.
    """
    if not settings.METRICS_ENABLED:
        return
    if not exporter.usable():
        # Fall back to no metrics rather than to a worker that dies on its
        # first increment. A dashboard with a gap is a smaller problem than a
        # queue that stops moving.
        os.environ.pop(exporter.MULTIPROC_ENV, None)
        logger.warning(
            "worker_metrics_directory_unwritable",
            component="worker",
            error_code="permission_denied",
        )
        return
    try:
        exporter.prepare_multiprocess_dir()
        exporter.serve(settings.WORKER_METRICS_PORT)
        logger.info(
            "worker_metrics_serving",
            component="worker",
            count=settings.WORKER_METRICS_PORT,
        )
    except OSError as exc:
        # A port clash must not stop a worker from doing its actual job.
        logger.warning(
            "worker_metrics_unavailable",
            component="worker",
            error_code=type(exc).__name__,
        )


@worker_process_shutdown.connect
def _reap_child_samples(pid=None, **_kwargs) -> None:
    """Drop a dead child's gauge samples.

    Left behind, they are exported forever — which reads as a worker that is
    still busy long after it was replaced.
    """
    if pid is not None:
        exporter.mark_process_dead(pid)


@task_prerun.connect
def _time_task_start(task_id=None, task=None, **_kwargs) -> None:
    _STARTED[task_id] = time.perf_counter()


@task_postrun.connect
def _time_task_end(task_id=None, task=None, state=None, **_kwargs) -> None:
    started = _STARTED.pop(task_id, None)
    if started is None:
        return
    metrics.job_stage_duration_seconds.labels(
        stage=(getattr(task, "name", "") or "unknown").rsplit(".", 1)[-1]
    ).observe(time.perf_counter() - started)
