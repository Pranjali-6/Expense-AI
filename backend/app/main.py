"""FastAPI application entry point."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import RequestContextMiddleware, SecurityHeadersMiddleware
from app.core.redis_client import close_redis
from app.db.session import dispose_engine
from app.observability import state

configure_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logger.info(
        "service_starting",
        component="api",
        # Whether AI is usable is worth knowing at a glance: a deployment that
        # silently fell back to deterministic-only categorization should be
        # visible in the first log line, not discovered from a dashboard.
        status="ai_enabled" if settings.ai_usable else "ai_disabled",
    )

    # Gauges describing durable state — review depth, ledger size, queue
    # depth — are refreshed on a timer rather than accumulated in memory, so
    # they read correctly after a restart. See app/observability/state.py.
    stop = asyncio.Event()
    refresher = (
        asyncio.create_task(state.refresh_forever(stop))
        if settings.METRICS_ENABLED else None
    )

    yield

    logger.info("service_stopping", component="api")
    if refresher is not None:
        stop.set()
        refresher.cancel()
        # Shutdown must not hang on a task that is mid-query, and must not
        # print a traceback for cancelling one on purpose.
        await asyncio.gather(refresher, return_exceptions=True)
    await close_redis()
    await dispose_engine()


app = FastAPI(
    title=settings.APP_NAME,
    version="0.1.0",
    description=(
        "Indian personal financial intelligence platform.\n\n"
        "Statement PDFs are extracted deterministically and validated before "
        "anything reaches the ledger. The LLM is an enrichment component: it "
        "never sees a raw document, never performs financial arithmetic, and "
        "never overrides a human correction."
    ),
    default_response_class=ORJSONResponse,
    docs_url="/docs" if not settings.is_production else None,
    redoc_url="/redoc" if not settings.is_production else None,
    openapi_url="/openapi.json" if not settings.is_production else None,
    lifespan=lifespan,
)

# Order matters: the outermost middleware runs first. Request context must be
# established before anything else so every downstream log line is correlated.
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestContextMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID", "X-CSRF-Token"],
    expose_headers=["X-Request-ID"],
    max_age=600,
)

register_exception_handlers(app)
app.include_router(api_router, prefix=settings.API_V1_PREFIX)


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {
        "service": settings.APP_NAME,
        "version": "0.1.0",
        "docs": "/docs" if not settings.is_production else "disabled",
    }
