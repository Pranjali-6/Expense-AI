"""API v1 router.

Endpoint modules are added phase by phase. Each router is mounted here rather
than on the app directly, so the ``/api/v1`` prefix and shared dependencies
have exactly one definition.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.endpoints import (
    accounts,
    assistant,
    audit,
    auth,
    budgets,
    categories,
    export,
    health,
    intelligence,
    jobs,
    notifications,
    privacy,
    statements,
    transactions,
)

api_router = APIRouter()

api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(statements.router)
api_router.include_router(jobs.router)
api_router.include_router(transactions.router)
api_router.include_router(accounts.router)
api_router.include_router(privacy.router)
api_router.include_router(intelligence.router)
api_router.include_router(budgets.router)
api_router.include_router(assistant.router)
api_router.include_router(export.router)
api_router.include_router(audit.router)
api_router.include_router(notifications.router)
api_router.include_router(categories.router)
