"""Application error types and handlers.

Two rules shape everything here:

1.  **Clients get a stable machine-readable code and a safe message.** Never a
    stack trace, never a database message, never the value that failed
    validation — a rejected amount echoed back in an error body is the same
    leak as logging it.

2.  **Logs get the code and the exception type, not the exception's rendered
    detail.** Tracebacks are still recorded (they are scrubbed by the logging
    pipeline) but we never hand a domain object's ``repr`` to a log call.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger

logger = get_logger(__name__)


class AppError(Exception):
    """Base class for expected, handled failures."""

    status_code: int = status.HTTP_400_BAD_REQUEST
    error_code: str = "app_error"
    message: str = "The request could not be completed."

    def __init__(
        self,
        message: str | None = None,
        *,
        error_code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.message
        self.error_code = error_code or self.error_code
        # `details` reaches the client, so it must contain only non-sensitive,
        # caller-supplied context (field names, limits, allowed values).
        self.details = details or {}
        super().__init__(self.message)


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    error_code = "not_found"
    message = "The requested resource does not exist."


class AuthenticationError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    error_code = "authentication_required"
    message = "Authentication is required."


class AuthorizationError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    error_code = "forbidden"
    message = "You do not have access to this resource."


class ValidationFailedError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    error_code = "validation_failed"
    message = "The submitted data is not valid."


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    error_code = "conflict"
    message = "The resource is in a conflicting state."


class RateLimitError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    error_code = "rate_limited"
    message = "Too many requests. Please slow down."


class UploadRejectedError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    error_code = "upload_rejected"
    message = "The uploaded file was rejected."


class PrivacyViolationError(AppError):
    """Raised when a payload bound for an LLM failed its post-build re-scan.

    This is a fail-closed control: the call is abandoned and the transaction is
    routed to human review rather than being retried with a cleaner payload.
    """

    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    error_code = "privacy_violation"
    message = "The operation was stopped by a privacy control."


class ServiceUnavailableError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    error_code = "service_unavailable"
    message = "A dependency is unavailable. Please retry shortly."


def _error_body(error_code: str, message: str, details: dict[str, Any] | None = None) -> dict:
    body: dict[str, Any] = {"error": {"code": error_code, "message": message}}
    if details:
        body["error"]["details"] = details
    return body


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
        logger.warning(
            "request_failed",
            error_code=exc.error_code,
            status_code=exc.status_code,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(exc.error_code, exc.message, exc.details),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body("http_error", str(exc.detail)),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Report *where* validation failed, never *what* was submitted.
        fields = [
            {
                "field": ".".join(str(part) for part in error.get("loc", ())[1:]),
                "issue": error.get("msg", "invalid"),
            }
            for error in exc.errors()
        ]
        logger.warning("request_validation_failed", error_code="validation_failed", count=len(fields))
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=jsonable_encoder(
                _error_body(
                    "validation_failed",
                    "The submitted data is not valid.",
                    {"fields": fields},
                )
            ),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
        # exc_info gives us the traceback (scrubbed downstream); we deliberately
        # do not interpolate str(exc) into the message.
        logger.error(
            "unhandled_exception",
            error_code=type(exc).__name__,
            exc_info=exc,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_error_body("internal_error", "An unexpected error occurred."),
        )
