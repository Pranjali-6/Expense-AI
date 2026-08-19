"""Indian statement date parsing.

**Day-first, always.** ``05/03/2024`` is 5 March on every Indian bank statement
and 3 May to a US-locale date library. Getting this wrong does not fail loudly:
it silently moves a third of transactions into the wrong month, which quietly
corrupts every monthly total, budget and trend in the product. So the formats
here are an explicit ordered list rather than a call to a fuzzy parser, and
``dateutil`` is used only with ``dayfirst=True`` as a last resort.

Two-digit years are windowed rather than assumed: a statement year of ``98``
is 1998, ``24`` is 2024. Banks do still print ``dd-MMM-yy``.
"""

from __future__ import annotations

import re
from datetime import date, datetime

_WHITESPACE = re.compile(r"[\s ]+")

# Ordered by how commonly Indian statements use them. Every one is
# unambiguously day-first or unambiguously ISO.
_FORMATS: tuple[str, ...] = (
    "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y",
    "%d/%m/%y", "%d-%m-%y", "%d.%m.%y",
    "%d %b %Y", "%d-%b-%Y", "%d/%b/%Y", "%d %B %Y",
    "%d %b %y", "%d-%b-%y", "%d/%b/%y",
    "%Y-%m-%d", "%Y/%m/%d",
    "%b %d, %Y", "%B %d, %Y", "%d%b%Y", "%d%b%y",
)

# A leading date at the start of a text line, for row detection.
_LEADING_DATE = re.compile(
    r"^\s*(\d{1,2}[-/. ](?:\d{1,2}|[A-Za-z]{3,9})[-/. ]\d{2,4})"
)


class DateParseError(ValueError):
    """Raised when a cell cannot be read as a date. Carries no cell content."""


def parse_date(raw: str | None, *, year_hint: int | None = None) -> date:
    """Parse a statement date cell, day-first.

    ``year_hint`` fills in a year for the layouts that print ``12 Mar`` with the
    year only in the statement header. It is applied **only** when the cell
    genuinely carries no year — never to override one that is printed.
    """
    if raw is None:
        raise DateParseError("empty date cell")

    text = _WHITESPACE.sub(" ", str(raw)).strip().rstrip(",")
    if not text:
        raise DateParseError("empty date cell")

    for fmt in _FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue

    # Year-less forms: "12 Mar", "12-Mar", "12/03".
    if year_hint is not None:
        for fmt in ("%d %b", "%d-%b", "%d/%b", "%d %B", "%d/%m", "%d-%m"):
            try:
                parsed = datetime.strptime(text, fmt).date()
                return parsed.replace(year=year_hint)
            except ValueError:
                continue

    # Last resort. dayfirst is non-negotiable.
    try:
        from dateutil.parser import parse as _dateutil_parse

        return _dateutil_parse(text, dayfirst=True, fuzzy=False).date()
    except Exception as exc:  # noqa: BLE001 - any failure means "not a date"
        raise DateParseError("unparseable date cell") from exc


def looks_like_date(raw: str | None) -> bool:
    """Cheap test for row/column detection."""
    if not raw:
        return False
    try:
        parse_date(raw)
        return True
    except DateParseError:
        return False


def leading_date(line: str) -> str | None:
    """Return the date token a text line starts with, if any.

    Text-layer row detection leans on this: in every statement layout a
    transaction row starts with its date, and a wrapped continuation line does
    not.
    """
    match = _LEADING_DATE.match(line)
    if not match:
        return None
    token = match.group(1)
    return token if looks_like_date(token) else None


def within(value: date, start: date | None, end: date | None, *, slack_days: int = 0) -> bool:
    """Is a transaction date inside the statement period?

    ``slack_days`` exists because a value date can legitimately fall a day or
    two outside the printed period. It is a validation signal, not a filter —
    an out-of-period row is flagged, never dropped.
    """
    from datetime import timedelta

    if start and value < start - timedelta(days=slack_days):
        return False
    if end and value > end + timedelta(days=slack_days):
        return False
    return True
