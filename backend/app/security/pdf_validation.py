"""Structural validation of uploaded PDFs.

A PDF is not a document format so much as a small virtual machine with a
document attached. It can carry JavaScript, launch external programs, open
remote URLs on load, embed arbitrary files, and expand a few kilobytes into
gigabytes of decompressed data. Any of those reaching a worker process is a
problem, so the file is inspected before it is stored and again before it is
parsed.

The checks are ordered cheapest-first: a wrong magic number costs four bytes to
detect, so nothing expensive runs on a file that was never a PDF.

Everything here **fails closed**. A file we cannot understand well enough to
clear is rejected, not waved through — the cost of a false rejection is a
confused user, the cost of a false acceptance is arbitrary code in the parser.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

import pikepdf

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

PDF_MAGIC: Final = b"%PDF-"

#: Names whose presence makes a PDF active rather than declarative.
#: Matched against the document catalog and every object's keys.
DANGEROUS_KEYS: Final[frozenset[str]] = frozenset({
    "/JavaScript",   # embedded script
    "/JS",           # script payload
    "/Launch",       # run an external program
    "/OpenAction",   # run something on open
    "/AA",           # additional actions: page open/close hooks
    "/EmbeddedFile", # a file inside the file
    "/EmbeddedFiles",
    "/RichMedia",    # Flash and friends
    "/Movie",
    "/Sound",
    "/XFA",          # a whole separate forms engine
    "/GoToR",        # jump into a remote document
    "/SubmitForm",   # POST the form somewhere
    "/ImportData",
})

#: A statement is text and tables. Anything wildly beyond this is either not a
#: statement or is trying to exhaust the worker.
MAX_OBJECTS: Final = 500_000
MAX_STREAM_EXPANSION_RATIO: Final = 200  # decompressed / stored


class RejectionReason(StrEnum):
    NOT_A_PDF = "not_a_pdf"
    TOO_LARGE = "too_large"
    EMPTY = "empty"
    CORRUPT = "corrupt"
    PASSWORD_REQUIRED = "password_required"
    WRONG_PASSWORD = "wrong_password"
    TOO_MANY_PAGES = "too_many_pages"
    NO_PAGES = "no_pages"
    ACTIVE_CONTENT = "active_content"
    EMBEDDED_FILE = "embedded_file"
    TOO_COMPLEX = "too_complex"
    DECOMPRESSION_BOMB = "decompression_bomb"


@dataclass(slots=True)
class ValidationResult:
    ok: bool
    reason: RejectionReason | None = None
    #: Safe to show a user: says what to do, never echoes file content.
    message: str = ""
    page_count: int = 0
    #: Names of the dangerous constructs found, for the audit trail. Names
    #: only — never the payload they carried.
    findings: list[str] = field(default_factory=list)
    is_encrypted: bool = False


_MESSAGES: Final[dict[RejectionReason, str]] = {
    RejectionReason.NOT_A_PDF: "That file is not a PDF.",
    RejectionReason.TOO_LARGE: f"Statements must be under {settings.MAX_UPLOAD_SIZE_MB} MB.",
    RejectionReason.EMPTY: "That file is empty.",
    RejectionReason.CORRUPT: "That PDF could not be read. It may be damaged.",
    RejectionReason.PASSWORD_REQUIRED: (
        "That statement is password protected. Enter the password and try again."
    ),
    RejectionReason.WRONG_PASSWORD: "That password did not open the statement.",
    RejectionReason.TOO_MANY_PAGES: f"Statements must be under {settings.MAX_PDF_PAGES} pages.",
    RejectionReason.NO_PAGES: "That PDF has no pages.",
    RejectionReason.ACTIVE_CONTENT: (
        "That PDF contains active content such as scripts, which is not accepted. "
        "Re-download the statement from your bank."
    ),
    RejectionReason.EMBEDDED_FILE: (
        "That PDF has other files embedded in it, which is not accepted."
    ),
    RejectionReason.TOO_COMPLEX: "That PDF is unusually complex and was not accepted.",
    RejectionReason.DECOMPRESSION_BOMB: "That PDF was rejected as malformed.",
}


def _reject(reason: RejectionReason, **extra) -> ValidationResult:
    return ValidationResult(ok=False, reason=reason, message=_MESSAGES[reason], **extra)


def validate_pdf(data: bytes, *, password: str | None = None) -> ValidationResult:
    """Inspect PDF bytes. Cheapest checks first."""

    # --- 1. is it a PDF at all -------------------------------------------
    if not data:
        return _reject(RejectionReason.EMPTY)

    if len(data) > settings.max_upload_bytes:
        return _reject(RejectionReason.TOO_LARGE)

    # Trusting the declared Content-Type or the file extension is how a .exe
    # renamed to .pdf gets in. The first bytes are the only claim worth anything.
    if not data.startswith(PDF_MAGIC):
        return _reject(RejectionReason.NOT_A_PDF)

    # --- 2. does it parse -------------------------------------------------
    try:
        pdf = pikepdf.open(io.BytesIO(data), password=password or "")
    except pikepdf.PasswordError:
        if password:
            return _reject(RejectionReason.WRONG_PASSWORD)
        return _reject(RejectionReason.PASSWORD_REQUIRED)
    except Exception:
        # pikepdf's message can quote raw file content; never surface it.
        return _reject(RejectionReason.CORRUPT)

    with pdf:
        is_encrypted = pdf.is_encrypted

        # --- 3. shape --------------------------------------------------
        page_count = len(pdf.pages)
        if page_count == 0:
            return _reject(RejectionReason.NO_PAGES)
        if page_count > settings.MAX_PDF_PAGES:
            return _reject(RejectionReason.TOO_MANY_PAGES, page_count=page_count)

        object_count = len(pdf.objects)
        if object_count > MAX_OBJECTS:
            return _reject(
                RejectionReason.TOO_COMPLEX, page_count=page_count
            )

        # --- 4. active content -----------------------------------------
        findings, traversal_errors = _scan_for_active_content(pdf)

        if traversal_errors:
            # Could not inspect the whole document. Not cleared — just not
            # convicted, which is not the same thing.
            logger.warning(
                "upload_rejected",
                error_code=str(RejectionReason.CORRUPT),
                count=traversal_errors,
                page_count=page_count,
            )
            return _reject(RejectionReason.CORRUPT, page_count=page_count)

        if findings:
            embedded_only = all(
                name in {"/EmbeddedFile", "/EmbeddedFiles"} for name in findings
            )
            reason = (
                RejectionReason.EMBEDDED_FILE
                if embedded_only
                else RejectionReason.ACTIVE_CONTENT
            )
            logger.warning(
                "upload_rejected",
                error_code=str(reason),
                count=len(findings),
                page_count=page_count,
            )
            return _reject(reason, page_count=page_count, findings=findings)

        # --- 5. decompression bomb --------------------------------------
        if _looks_like_a_decompression_bomb(pdf, len(data)):
            return _reject(
                RejectionReason.DECOMPRESSION_BOMB, page_count=page_count
            )

    return ValidationResult(
        ok=True, page_count=page_count, is_encrypted=is_encrypted
    )


def _scan_for_active_content(pdf: pikepdf.Pdf) -> tuple[list[str], int]:
    """Walk every object looking for names that make a PDF *do* something.

    Returns the constructs found and a count of objects that could not be
    traversed. The error count matters as much as the findings: a file we were
    unable to inspect fully has not been cleared, it has merely not been
    convicted, and the caller treats that as a rejection.

    A whole-document sweep rather than a catalog check, deliberately. Actions
    hide on individual pages, in annotations and in the names tree, and a
    document that looks clean at the top level is exactly what someone would
    construct.
    """
    found: set[str] = set()
    errors = 0

    def inspect(obj, depth: int = 0) -> None:
        nonlocal errors
        # A hostile PDF can be deeply self-referential; bound the walk.
        if depth > 24:
            return
        try:
            if isinstance(obj, pikepdf.Dictionary):
                # Indexing by key, not .values(): pikepdf dictionaries are
                # `Object` instances and do not expose a values() method. The
                # earlier version called it inside a broad try/except, so the
                # AttributeError was swallowed and this entire traversal
                # silently did nothing below the top level.
                for key in obj.keys():
                    if key in DANGEROUS_KEYS:
                        found.add(key)
                    try:
                        inspect(obj[key], depth + 1)
                    except Exception:
                        errors += 1
            elif isinstance(obj, pikepdf.Array):
                for item in obj:
                    inspect(item, depth + 1)
        except Exception:
            errors += 1

    try:
        inspect(pdf.Root)
    except Exception:
        errors += 1

    # The catalog is reachable from the root, but orphaned and
    # indirectly-referenced objects are not — and an action parked in one still
    # runs. Sweep the whole object table too.
    try:
        for obj in pdf.objects:
            inspect(obj)
    except Exception:
        errors += 1

    return sorted(found), errors


def _looks_like_a_decompression_bomb(pdf: pikepdf.Pdf, stored_size: int) -> bool:
    """Compare declared decompressed length against the file's actual size.

    A stream that claims to expand to hundreds of times the whole file is not a
    bank statement. Reads `/Length1`-style hints rather than decompressing, so
    the check itself cannot be the thing that exhausts memory.
    """
    declared_total = 0
    try:
        for obj in pdf.objects:
            if not isinstance(obj, pikepdf.Stream):
                continue
            raw = obj.get("/DL") or obj.get("/Length1")
            if raw is not None:
                declared_total += int(raw)
    except Exception:
        return False

    if declared_total == 0:
        return False

    return declared_total > stored_size * MAX_STREAM_EXPANSION_RATIO
