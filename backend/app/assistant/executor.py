"""Running one tool, on behalf of a caller the model cannot name.

Everything the model asked for is untrusted input: the tool name, the argument
names, the argument values. Each is checked here before anything runs, and the
one thing that is never checked — because it is never supplied — is who the
caller is. The session arrives already scoped to a tenant by the FastAPI
dependency that read the access token, so a tool cannot be pointed anywhere
else. That is the whole authorization model, and it is an absence rather than a
check: there is no parameter to get wrong.

Four gates, in order:

1. **Known tool.** Names come from a fixed registry. An unrecognised name is a
   refusal, not a fuzzy match onto the nearest real tool.
2. **Valid arguments.** Pydantic with ``extra="forbid"``. This is where
   ``{"tenant_id": "..."}`` dies — not with a warning, with a validation error,
   because the field does not exist.
3. **The tool runs**, against the scoped session, using the Intelligence
   Engine's own functions.
4. **The result is re-scanned** before any of it crosses the perimeter. A
   detector hit or an injection-shaped string aborts the whole answer.

A failure at any gate is reported to the model as a short, contentless error
string. It never carries a stack trace, a column name or a value — a model that
is told "column t.merchant does not exist" has just been handed a piece of the
schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.assistant import redaction
from app.assistant.period import PeriodError
from app.assistant.tools import BY_NAME, ToolResult
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Execution:
    result: ToolResult | None
    #: A code, never a message from an exception.
    error_code: str | None = None
    #: What the model is told when it failed. Short and contentless.
    error_message: str | None = None

    @property
    def ok(self) -> bool:
        return self.result is not None


_MESSAGES = {
    "unknown_tool": "That function does not exist.",
    "invalid_arguments": "Those arguments are not valid for this function.",
    "invalid_period": "A period must be written as YYYY-MM or YYYY.",
    "tool_failed": "That function could not be run.",
    "blocked": "That result could not be returned.",
}


def _failure(code: str) -> Execution:
    return Execution(result=None, error_code=code, error_message=_MESSAGES[code])


async def execute(
    session: AsyncSession,
    *,
    name: str,
    arguments: dict[str, Any] | None,
    default_month: date,
) -> Execution:
    """Validate and run one tool call."""
    tool = BY_NAME.get(name)
    if tool is None:
        logger.info("assistant_unknown_tool", stage="assistant", error_code="unknown_tool")
        return _failure("unknown_tool")

    try:
        args = tool.args_model(**(arguments or {}))
    except ValidationError:
        # Includes the case that matters most: an attempt to supply an identity
        # field. `extra="forbid"` makes that a validation error rather than an
        # ignored key, so it is visible here instead of silently discarded.
        logger.info(
            "assistant_invalid_arguments",
            stage="assistant",
            error_code="invalid_arguments",
        )
        return _failure("invalid_arguments")

    try:
        result = await tool.runner(session, args, default_month)
    except PeriodError:
        return _failure("invalid_period")
    except Exception as exc:
        logger.warning(
            "assistant_tool_failed", stage="assistant", error_code=type(exc).__name__
        )
        return _failure("tool_failed")

    verified = redaction.verify(result.model_view)
    if not verified.ok:
        logger.error(
            "assistant_result_blocked",
            stage="assistant",
            error_code=verified.blocked_by,
        )
        return Execution(
            result=None,
            error_code=verified.blocked_by,
            error_message=_MESSAGES["blocked"],
        )

    return Execution(result=result)
