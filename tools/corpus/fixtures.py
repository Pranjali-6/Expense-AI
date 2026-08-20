"""Opening a real-corpus fixture.

Real statements arrive locked far more often than not, so anything that reads
`tests/fixtures/real/` has to be able to decrypt. Both readers — the accuracy
harness and the ground-truth builder — go through here, so there is one answer
to "how do I open this file" rather than two that can drift.
"""

from __future__ import annotations

from pathlib import Path


class FixtureLocked(RuntimeError):
    """A fixture needs a password the tooling was not given."""


def fixture_bytes(pdf_path: Path) -> bytes:
    """Read a fixture, decrypting it first when it is password protected.

    The password comes from ``REAL_CORPUS_PASSWORDS`` keyed by filename — one
    password per file, because a drop of statements from four banks has four
    different passwords.

    The decrypted bytes are never written back to disk: the fixture on disk
    stays exactly as the bank issued it, and the plaintext lives only for the
    length of this call.
    """
    from app.core.config import get_settings
    from app.security.pdf_validation import (
        PdfPasswordError,
        RejectionReason,
        unlock_pdf,
        validate_pdf,
    )

    data = pdf_path.read_bytes()

    if validate_pdf(data).reason is not RejectionReason.PASSWORD_REQUIRED:
        return data

    password = get_settings().real_corpus_passwords.get(pdf_path.name)
    if not password:
        raise FixtureLocked(
            f"{pdf_path.name} is password protected. Add it to "
            f"REAL_CORPUS_PASSWORDS in .env as `{pdf_path.name}:<password>`."
        )

    try:
        return unlock_pdf(data, password=password)
    except PdfPasswordError:
        raise FixtureLocked(
            f"The REAL_CORPUS_PASSWORDS entry for {pdf_path.name} did not open it."
        ) from None
