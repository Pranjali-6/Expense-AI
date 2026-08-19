"""Health and metrics endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Response, status
from app.core.config import settings
from app.observability import exporter
from app.observability.health import HealthStatus, check_health

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live", summary="Liveness probe")
async def live() -> dict[str, str]:
    """Is the process up? Touches nothing — a database blip must not restart
    a perfectly healthy API container."""
    return {"status": "alive", "service": settings.SERVICE_NAME}


@router.get("/ready", summary="Readiness probe")
async def ready(response: Response) -> dict:
    """Can the process serve traffic? Checks every dependency."""
    report = await check_health(include_workers=False)
    if report.status is HealthStatus.UNHEALTHY:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return report.to_dict()


@router.get("", summary="Full health report")
async def health(response: Response) -> dict:
    """Everything: API, PostgreSQL, Redis, MinIO and the Celery workers."""
    report = await check_health(include_workers=True)
    if report.status is HealthStatus.UNHEALTHY:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return report.to_dict()


@router.get("/metrics", summary="Prometheus metrics", include_in_schema=False)
async def prometheus_metrics() -> Response:
    """Exposition for whichever mode this process runs in.

    Rendered through the exporter rather than ``generate_latest()`` directly, so
    the same endpoint is correct whether the API is one process or several —
    the difference is a directory of mmap files, not a code path.
    """
    if not settings.METRICS_ENABLED:
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    payload, content_type = exporter.render()
    return Response(content=payload, media_type=content_type)
