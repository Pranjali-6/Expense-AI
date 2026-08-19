"""Structured logging with mandatory redaction.

The platform's logging policy is a hard invariant, not a guideline:

    Logs may contain identifiers. Logs may never contain money, descriptions,
    merchant names, account or card numbers, UPI IDs, personal details,
    filenames, PDF content, or AI prompts and responses.

Three mechanisms enforce it, in order of how much they are relied upon:

1.  **Field allow-list** (primary). A log event may only carry keys from
    :data:`ALLOWED_FIELDS`. Anything else is dropped before rendering, and only
    its *name* is reported. This is the mechanism that actually holds, because
    it fails safe for data nobody anticipated.

2.  **Value redaction** (secondary). Every surviving string — including the
    event message itself and any rendered traceback — is passed through the
    same detector set the privacy gateway uses, plus a monetary detector.
    This catches sensitive data interpolated into a message string.

3.  **Test** (verification). ``tests/security/test_log_leakage.py`` runs a full
    upload-to-ledger pipeline, captures every log line, and asserts nothing
    sensitive appears. Mechanisms 1 and 2 are the design; the test is the proof.

Third-party loggers (uvicorn, SQLAlchemy, Celery, asyncpg) are bridged into the
same pipeline, so their output is redacted too. A library that helpfully logs a
failing SQL statement with bound parameters is exactly the leak this prevents.
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from typing import Any, Final, MutableMapping

import structlog

from app.core.config import settings
from app.privacy.detectors import redact_for_logs

# --------------------------------------------------------------------------- #
# Field allow-list
# --------------------------------------------------------------------------- #

#: Correlation and domain identifiers. Opaque UUIDs and enum labels only —
#: nothing here can carry a monetary value or a human-readable description.
_IDENTITY_FIELDS: Final[frozenset[str]] = frozenset({
    "request_id",
    "user_id",
    "tenant_id",
    "job_id",
    "statement_id",
    "account_id",
    "transaction_id",
})

#: Operational context. Every one of these is a bounded enum, a count, or a
#: duration. Adding to this set is a deliberate decision, not a convenience.
_OPERATIONAL_FIELDS: Final[frozenset[str]] = frozenset({
    "bank_code",
    "stage",
    "status",
    "status_code",
    "duration_ms",
    "count",
    "error_code",
    "model_name",
    "component",
    "service",
    "method",
    "path",
    "queue",
    "task_name",
    "attempt",
    "provider",
    "category_source",
    "review_status",
    "page_count",
    "file_count",
    "outcome",
})

#: structlog / stdlib machinery that must survive the filter.
#: ``_record`` and ``_from_structlog`` are ProcessorFormatter internals —
#: dropping them makes ``remove_processors_meta`` raise KeyError downstream.
_META_FIELDS: Final[frozenset[str]] = frozenset({
    "event",
    "level",
    "timestamp",
    "logger",
    "exception",
    "exc_info",
    "stack_info",
    "dropped_fields",
    "environment",
    "_record",
    "_from_structlog",
})

ALLOWED_FIELDS: Final[frozenset[str]] = (
    _IDENTITY_FIELDS | _OPERATIONAL_FIELDS | _META_FIELDS
)

#: Fields exempt from *value* redaction.
#:
#: These are structurally incapable of carrying sensitive data — UUIDs we
#: generated, an ISO timestamp, a bounded enum. Running the scrubber over them
#: does active harm: a UUID's digit groups look like account numbers and a
#: timestamp's microseconds look like a long digit run, so a correlation id
#: comes out as `[REDACTED:LONG_DIGIT_RUN]-1111-…` and stops correlating
#: anything. The allow-list is what protects these fields; the scrubber exists
#: for free text.
_NO_REDACT_FIELDS: Final[frozenset[str]] = _IDENTITY_FIELDS | frozenset({
    "timestamp",
    "level",
    "logger",
    "service",
    "environment",
    "duration_ms",
    "count",
    "status_code",
    "page_count",
    "file_count",
    "attempt",
})


# --------------------------------------------------------------------------- #
# Request-scoped context
# --------------------------------------------------------------------------- #

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_user_id: ContextVar[str | None] = ContextVar("user_id", default=None)
_tenant_id: ContextVar[str | None] = ContextVar("tenant_id", default=None)
_job_id: ContextVar[str | None] = ContextVar("job_id", default=None)

_CONTEXT_VARS: Final = {
    "request_id": _request_id,
    "user_id": _user_id,
    "tenant_id": _tenant_id,
    "job_id": _job_id,
}


def bind_context(
    *,
    request_id: str | None = None,
    user_id: str | None = None,
    tenant_id: str | None = None,
    job_id: str | None = None,
) -> None:
    """Attach correlation identifiers to every subsequent log line."""
    for name, value in (
        ("request_id", request_id),
        ("user_id", user_id),
        ("tenant_id", tenant_id),
        ("job_id", job_id),
    ):
        if value is not None:
            _CONTEXT_VARS[name].set(str(value))


def clear_context() -> None:
    for var in _CONTEXT_VARS.values():
        var.set(None)


def get_context() -> dict[str, str]:
    return {name: value for name, var in _CONTEXT_VARS.items() if (value := var.get())}


# --------------------------------------------------------------------------- #
# Processors
# --------------------------------------------------------------------------- #

def _inject_context(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for name, var in _CONTEXT_VARS.items():
        value = var.get()
        if value is not None and name not in event_dict:
            event_dict[name] = value
    return event_dict


def _inject_service(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    event_dict.setdefault("service", settings.SERVICE_NAME)
    event_dict.setdefault("environment", settings.ENVIRONMENT)
    return event_dict


def _enforce_allowlist(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Drop every key that is not explicitly permitted.

    Only the *names* of dropped keys are reported. A key name like
    ``merchant`` is harmless; its value is the whole problem.
    """
    dropped = [key for key in event_dict if key not in ALLOWED_FIELDS]
    for key in dropped:
        del event_dict[key]
    if dropped:
        event_dict["dropped_fields"] = sorted(dropped)
    return event_dict


def _redact_values(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Scrub every free-text value, including the message and any traceback."""
    for key, value in event_dict.items():
        if key in _NO_REDACT_FIELDS:
            continue
        if isinstance(value, str):
            event_dict[key] = redact_for_logs(value)
        elif isinstance(value, (list, tuple)):
            event_dict[key] = [
                redact_for_logs(item) if isinstance(item, str) else item for item in value
            ]
    return event_dict


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

def configure_logging() -> None:
    """Install the logging pipeline. Safe to call more than once."""
    level = getattr(logging, settings.LOG_LEVEL, logging.INFO)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _inject_context,
        _inject_service,
        # Render exceptions to a string *before* redaction, so tracebacks are
        # scrubbed rather than dropped. Stack frames are diagnostic gold; the
        # exception message is where data leaks.
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        # Order is deliberate: allow-list first (cheap, definitive), then
        # redaction of what survived (thorough, defence in depth).
        _enforce_allowlist,
        _redact_values,
    ]

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if settings.LOG_FORMAT == "json"
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    # structlog emits through stdlib logging rather than writing directly, so
    # there is exactly one sink and exactly one formatter. Our own log calls and
    # a third-party library's both end up passing through the allow-list and the
    # redactor — which is the whole point.
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Libraries that log data we must not keep.
    #   sqlalchemy.engine — emits SQL with bound parameters at INFO
    #   asyncpg           — embeds column values in error text
    #   uvicorn.access    — superseded by our own request middleware
    #
    # The PDF and OCR stack is clamped hardest, and it is the clamp that matters
    # most: pdfminer logs page dictionaries and text runs at DEBUG, so a
    # `LOG_LEVEL=DEBUG` set to diagnose a parsing problem would dump the
    # statement's contents — merchant names, amounts, account numbers — straight
    # into the log stream. That is exactly the moment someone turns DEBUG on,
    # which makes it the most likely way this policy would have been broken.
    # Found by tests/security/test_log_leakage.py, which runs a whole import at
    # DEBUG and greps the result for the fixture's own values.
    for noisy, noisy_level in (
        ("sqlalchemy.engine", logging.WARNING),
        ("sqlalchemy.pool", logging.WARNING),
        ("asyncpg", logging.WARNING),
        ("uvicorn.access", logging.WARNING),
        ("uvicorn.error", logging.INFO),
        ("celery", logging.INFO),
        ("multipart", logging.WARNING),
        ("botocore", logging.WARNING),
        ("urllib3", logging.WARNING),
        # --- the document stack ---------------------------------------------
        ("pdfminer", logging.WARNING),
        ("pdfplumber", logging.WARNING),
        ("camelot", logging.WARNING),
        ("fitz", logging.WARNING),
        ("pypdf", logging.WARNING),
        ("pikepdf", logging.WARNING),
        ("PIL", logging.WARNING),
        ("pytesseract", logging.WARNING),
        ("google_genai", logging.WARNING),
        ("google.genai", logging.WARNING),
        ("httpcore", logging.WARNING),
        ("httpx", logging.WARNING),
    ):
        logging.getLogger(noisy).setLevel(noisy_level)
        logging.getLogger(noisy).propagate = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[return-value]
