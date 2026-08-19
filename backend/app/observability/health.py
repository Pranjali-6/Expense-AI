"""Dependency health checks.

Two distinct questions, deliberately kept separate:

*   **liveness** — is this process running? Used by the container healthcheck.
    It must never touch a dependency, or a brief database blip restarts an
    otherwise healthy API.
*   **readiness** — can this process serve traffic? Checks every dependency.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.core.config import settings
from app.core.redis_client import ping_redis
from app.core.storage import ping_storage
from app.db.session import ping_database
from app.observability import metrics

_PROBE_TIMEOUT_SECONDS = 4.0


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass(slots=True)
class ComponentHealth:
    name: str
    healthy: bool
    latency_ms: float | None = None
    detail: str | None = None


@dataclass(slots=True)
class HealthReport:
    status: HealthStatus
    components: list[ComponentHealth] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "service": settings.SERVICE_NAME,
            "environment": settings.ENVIRONMENT,
            "ai_enabled": settings.ai_usable,
            "components": {
                component.name: {
                    "healthy": component.healthy,
                    "latency_ms": component.latency_ms,
                    **({"detail": component.detail} if component.detail else {}),
                }
                for component in self.components
            },
        }


async def _timed(name: str, probe) -> ComponentHealth:
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        result = await asyncio.wait_for(probe(), timeout=_PROBE_TIMEOUT_SECONDS)
        healthy = bool(result)
        detail = None
    except asyncio.TimeoutError:
        healthy, detail = False, "probe timed out"
    except Exception:
        # The exception's message may carry connection strings or server text.
        healthy, detail = False, "probe failed"

    latency_ms = round((loop.time() - started) * 1000, 2)
    metrics.dependency_up.labels(dependency=name).set(1 if healthy else 0)
    return ComponentHealth(name=name, healthy=healthy, latency_ms=latency_ms, detail=detail)


async def _ping_storage_async() -> bool:
    # The MinIO SDK is synchronous; keep it off the event loop.
    return await asyncio.to_thread(ping_storage)


async def _ping_workers() -> bool:
    """Ask Celery whether any worker answers.

    Imported lazily: the API image does not carry the extraction toolchain, and
    a worker-side import failure must not take the API's health check with it.
    """

    def _probe() -> bool:
        from workers.celery_app import celery_app

        replies = celery_app.control.ping(timeout=2.0)
        return bool(replies)

    return await asyncio.to_thread(_probe)


async def check_health(*, include_workers: bool = True) -> HealthReport:
    probes: list[tuple[str, Any]] = [
        ("postgres", ping_database),
        ("redis", ping_redis),
        ("minio", _ping_storage_async),
    ]
    if include_workers:
        probes.append(("workers", _ping_workers))

    components = await asyncio.gather(*(_timed(name, probe) for name, probe in probes))
    components = list(components)

    # Postgres is the source of truth: without it nothing is servable. The
    # others degrade specific features rather than the whole API, so their
    # failure is reported as degraded, not unhealthy.
    critical = {"postgres"}
    failed = {component.name for component in components if not component.healthy}

    if failed & critical:
        status = HealthStatus.UNHEALTHY
    elif failed:
        status = HealthStatus.DEGRADED
    else:
        status = HealthStatus.HEALTHY

    return HealthReport(status=status, components=components)
