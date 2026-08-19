"""The assistant API.

Two endpoints and one guarantee: the caller's identity comes from the access
token and is passed to the orchestrator as an argument the request body cannot
influence. There is no tenant field in the request schema — not validated away,
absent — which is the same shape the tool arguments have, for the same reason.

Rate limited on its own scope rather than the general API one. An assistant
question can cost real money and several seconds of a worker's attention, so
120 a minute is the wrong ceiling for it even though it is the right one for
reading a page of the ledger.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from app.assistant import deterministic, orchestrator, tools
from app.core.config import settings
from app.core.deps import CurrentUser, TenantSession
from app.core.errors import RateLimitError
from app.core.rate_limit import RateLimitScope, check_rate_limit
from app.privacy.allowlist import ALLOWED_FIELDS

router = APIRouter(prefix="/assistant", tags=["assistant"])


class QueryRequest(BaseModel):
    """What a caller may ask for.

    ``extra="forbid"`` for the same reason the tool arguments use it: a request
    body is attacker-controlled, and a field that is silently ignored today is
    a field someone wires up by accident tomorrow.
    """

    model_config = ConfigDict(extra="forbid")

    question: str = Field(default="", max_length=orchestrator.MAX_QUESTION_LENGTH)
    #: Set when the user tapped a canned question rather than typing one.
    suggestion_id: str | None = Field(default=None, max_length=40)


async def _throttle(current_user: CurrentUser) -> None:
    result = await check_rate_limit(RateLimitScope.ASSISTANT, str(current_user.id))
    if not result.allowed:
        raise RateLimitError(
            "Too many questions in a short time. Please wait a moment.",
            details={"retry_after_seconds": result.retry_after_seconds},
        )


@router.post("/query", summary="Ask a question about your own money")
async def query(
    payload: QueryRequest,
    session: TenantSession,
    current_user: CurrentUser,
    _throttled: None = Depends(_throttle),
) -> dict[str, Any]:
    """Answer one question.

    ``tenant_id`` comes from ``current_user``, which comes from the signed
    token. It is not in ``QueryRequest`` and there is no route by which a
    caller could put it there.
    """
    answer = await orchestrator.answer(
        session,
        tenant_id=current_user.tenant_id,
        question=payload.question,
        suggestion_id=payload.suggestion_id,
    )
    return answer.as_dict()


@router.get("/suggestions", summary="Questions that can be answered right now")
async def suggestions(session: TenantSession) -> dict[str, Any]:
    """The canned questions, and an honest account of what backs them.

    ``ai_enabled`` is reported so the UI can say which mode it is in rather
    than leaving the user to infer it from how the answers read. The tool list
    and the allow-listed fields are rendered from the code's own definitions,
    not from a copy — a screen that claims a narrower perimeter than the code
    enforces is worse than no screen.
    """
    return {
        "ai_enabled": settings.ai_usable,
        "suggestions": [
            {"id": item.id, "question": item.question, "tool": item.tool}
            for item in deterministic.SUGGESTIONS
        ],
        "tools": [
            {"name": tool.name, "description": tool.description}
            for tool in tools.REGISTRY
        ],
        "allowed_fields": list(ALLOWED_FIELDS),
        "max_tool_calls": orchestrator.MAX_TOOL_CALLS,
    }
