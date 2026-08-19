"""Sensitive-data detectors.

This is the single detector set used by *both* consumers that must never leak:

  1. ``app.core.logging``  — scrubs anything heading for a log sink.
  2. ``app.privacy.gateway`` (P6) — scrubs and then re-verifies any payload
     heading for an LLM, failing closed on a hit.

There is deliberately one implementation. Two copies of "what counts as
sensitive" would drift, and the drift would be invisible until it mattered.

Design notes
------------
*   Detectors err towards false positives. Redacting a harmless reference
    number costs nothing; missing a card number costs everything.
*   Checksummed identifiers (Aadhaar, card PANs) are validated rather than
    matched purely by shape, so ordinary 12- and 16-digit reference numbers
    are not all mangled into uselessness — but they still fall to the generic
    long-digit-run detector, which is the intended backstop.
*   Order matters: specific, structured identifiers are matched before the
    broad numeric catch-alls, so redaction labels stay informative.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Iterator


class PIIKind(StrEnum):
    """What a detector believes it found."""

    PAN = "PAN"                     # Indian income-tax Permanent Account Number
    AADHAAR = "AADHAAR"
    IFSC = "IFSC"
    GSTIN = "GSTIN"
    CARD_NUMBER = "CARD_NUMBER"
    ACCOUNT_NUMBER = "ACCOUNT_NUMBER"
    UPI_ID = "UPI_ID"
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    IP_ADDRESS = "IP_ADDRESS"
    MONETARY = "MONETARY"           # log sinks only; see LOG_DETECTORS
    LONG_DIGIT_RUN = "LONG_DIGIT_RUN"


@dataclass(frozen=True, slots=True)
class Detection:
    kind: PIIKind
    start: int
    end: int
    text: str


# --------------------------------------------------------------------------- #
# Checksums
# --------------------------------------------------------------------------- #

_VERHOEFF_D: Final = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)

_VERHOEFF_P: Final = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)


def is_valid_aadhaar(digits: str) -> bool:
    """Verhoeff check used by UIDAI. Aadhaar never starts with 0 or 1."""
    if len(digits) != 12 or not digits.isdigit() or digits[0] in "01":
        return False
    checksum = 0
    for position, digit in enumerate(reversed(digits)):
        checksum = _VERHOEFF_D[checksum][_VERHOEFF_P[position % 8][int(digit)]]
    return checksum == 0


def is_valid_luhn(digits: str) -> bool:
    """Luhn mod-10, used by essentially every payment card scheme."""
    if not digits.isdigit() or not 13 <= len(digits) <= 19:
        return False
    total = 0
    for position, digit in enumerate(reversed(digits)):
        value = int(digit)
        if position % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


# --------------------------------------------------------------------------- #
# Patterns
# --------------------------------------------------------------------------- #

_PAN_RE: Final = re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b")
_AADHAAR_RE: Final = re.compile(r"\b([2-9]\d{3})[\s-]?(\d{4})[\s-]?(\d{4})\b")
_IFSC_RE: Final = re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b")
_GSTIN_RE: Final = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][0-9A-Z]Z[0-9A-Z]\b")
_CARD_RE: Final = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
_UPI_RE: Final = re.compile(r"\b[\w][\w.\-]{1,63}@[a-zA-Z][a-zA-Z0-9]{1,63}\b")
_EMAIL_RE: Final = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_PHONE_RE: Final = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{9}(?!\d)")
_IP_RE: Final = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_ACCOUNT_RE: Final = re.compile(r"(?<!\d)\d{9,18}(?!\d)")
_LONG_DIGITS_RE: Final = re.compile(r"(?<!\d)\d{6,}(?!\d)")

# Log sinks only. Financial values must never reach a log line, and a rupee
# figure is usually short enough to slip past the long-digit-run detector.
_MONETARY_RE: Final = re.compile(
    r"(?:(?:₹|INR|Rs\.?)\s*[\d,]+(?:\.\d{1,2})?)"      # ₹1,23,456.78
    r"|(?<![\w.])\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?(?![\w.])"  # 1,23,456.78
    r"|(?<![\w.])\d+\.\d{2}(?![\w.])",                  # 450.00
    re.IGNORECASE,
)


def _digits_only(text: str) -> str:
    return re.sub(r"\D", "", text)


def _find_aadhaar(text: str) -> Iterator[Detection]:
    for match in _AADHAAR_RE.finditer(text):
        if is_valid_aadhaar(_digits_only(match.group(0))):
            yield Detection(PIIKind.AADHAAR, match.start(), match.end(), match.group(0))


def _find_card(text: str) -> Iterator[Detection]:
    for match in _CARD_RE.finditer(text):
        if is_valid_luhn(_digits_only(match.group(0))):
            yield Detection(PIIKind.CARD_NUMBER, match.start(), match.end(), match.group(0))


def _simple(kind: PIIKind, pattern: re.Pattern[str]):
    def finder(text: str) -> Iterator[Detection]:
        for match in pattern.finditer(text):
            yield Detection(kind, match.start(), match.end(), match.group(0))

    return finder


# Evaluated in order; earlier detections win overlapping spans.
_DETECTOR_CHAIN: Final = (
    _simple(PIIKind.PAN, _PAN_RE),
    _find_aadhaar,
    _simple(PIIKind.IFSC, _IFSC_RE),
    _simple(PIIKind.GSTIN, _GSTIN_RE),
    _find_card,
    _simple(PIIKind.EMAIL, _EMAIL_RE),
    _simple(PIIKind.UPI_ID, _UPI_RE),
    _simple(PIIKind.PHONE, _PHONE_RE),
    _simple(PIIKind.IP_ADDRESS, _IP_RE),
    _simple(PIIKind.ACCOUNT_NUMBER, _ACCOUNT_RE),
    _simple(PIIKind.LONG_DIGIT_RUN, _LONG_DIGITS_RE),
)

# The log redactor additionally strips monetary values. The privacy gateway
# does not use this: it never receives a free-text amount in the first place,
# it receives a bucket label.
_LOG_DETECTOR_CHAIN: Final = (
    _simple(PIIKind.MONETARY, _MONETARY_RE),
    *_DETECTOR_CHAIN,
)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def _scan(text: str, chain) -> list[Detection]:
    """Run a detector chain, discarding spans already claimed by an earlier
    (more specific) detector."""
    found: list[Detection] = []
    claimed: list[tuple[int, int]] = []

    for detector in chain:
        for detection in detector(text):
            if any(detection.start < end and start < detection.end for start, end in claimed):
                continue
            found.append(detection)
            claimed.append((detection.start, detection.end))

    found.sort(key=lambda d: d.start)
    return found


def scan(text: str) -> list[Detection]:
    """Every sensitive span in ``text``. Empty list means the text is clean."""
    if not text:
        return []
    return _scan(text, _DETECTOR_CHAIN)


def scan_for_logs(text: str) -> list[Detection]:
    """As :func:`scan`, plus monetary values."""
    if not text:
        return []
    return _scan(text, _LOG_DETECTOR_CHAIN)


def contains_pii(text: str) -> bool:
    """True if anything sensitive is present.

    Used by the privacy gateway's post-build re-scan, where a True result
    aborts the LLM call outright rather than trying to clean up.
    """
    return bool(scan(text))


def _apply(text: str, detections: list[Detection]) -> str:
    if not detections:
        return text
    out: list[str] = []
    cursor = 0
    for detection in detections:
        out.append(text[cursor:detection.start])
        out.append(f"[REDACTED:{detection.kind}]")
        cursor = detection.end
    out.append(text[cursor:])
    return "".join(out)


def redact(text: str) -> str:
    """Replace every sensitive span with a typed placeholder."""
    return _apply(text, scan(text))


def redact_for_logs(text: str) -> str:
    """Log-sink redaction: everything :func:`redact` removes, plus amounts."""
    return _apply(text, scan_for_logs(text))
