"""HTTP middleware: request correlation, access logging, security headers."""

from __future__ import annotations

import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.core.config import settings
from app.core.logging import bind_context, clear_context, get_logger
from app.observability import metrics

logger = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request id, binds logging context, and emits the access log.

    This replaces uvicorn's access log (silenced in ``configure_logging``)
    because uvicorn's version logs the raw query string, which is exactly the
    kind of thing our policy forbids reaching a sink unfiltered.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        request.state.request_id = request_id

        clear_context()
        bind_context(request_id=request_id)

        route_template = request.scope.get("path", "")
        started = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.exception(
                "request_errored",
                method=request.method,
                path=route_template,
                duration_ms=duration_ms,
            )
            metrics.http_requests_total.labels(
                method=request.method, path=route_template, status="500"
            ).inc()
            raise
        finally:
            clear_context()

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers[REQUEST_ID_HEADER] = request_id

        # Health probes fire constantly and would drown the signal.
        if not route_template.endswith(("/health/live", "/health/ready")):
            logger.info(
                "request_completed",
                method=request.method,
                path=route_template,
                status_code=response.status_code,
                duration_ms=duration_ms,
            )

        metrics.http_requests_total.labels(
            method=request.method,
            path=route_template,
            status=str(response.status_code),
        ).inc()
        metrics.http_request_duration_seconds.labels(
            method=request.method, path=route_template
        ).observe(duration_ms / 1000)

        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Defence in depth — nginx sets these too, but the API may be reached
    directly in development or behind a different proxy in production."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        # Financial responses must not be cached anywhere.
        response.headers.setdefault("Cache-Control", "no-store")
        if settings.is_production:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response
