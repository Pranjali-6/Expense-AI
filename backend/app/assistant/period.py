"""Turning what a person said into a date range.

Deliberately tiny and deliberately strict. The model may express a period as
``2025-03`` or ``2025`` and nothing else — no "last month", no "Q3", no free
text. Relative language is resolved *before* the model sees anything: the
system instruction states which month is current, so "this month" becomes a
literal ``YYYY-MM`` in the tool call rather than something this module has to
interpret.

That split matters. Date interpretation is where a natural-language finance
tool quietly goes wrong — "last month" on the 1st, a financial year that starts
in April, a month with no data. Keeping the parser to two literal shapes means
a period is either exactly what was asked for or a validation error, never a
plausible-looking guess.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date

_MONTH = re.compile(r"^(\d{4})-(\d{2})$")
_YEAR = re.compile(r"^(\d{4})$")

#: Bank statements do not go back to the Raj, and a four-digit year in the far
#: future is a model error rather than a request.
_MIN_YEAR = 1990
_MAX_YEAR = 2100


class PeriodError(ValueError):
    """The period could not be parsed. Never guessed at."""


@dataclass(frozen=True, slots=True)
class Period:
    first: date
    last: date
    #: What to call it in a sentence: "March 2025" or "2025".
    label: str
    #: Whether this is a single month, which some tools require.
    is_month: bool

    @property
    def as_bounds(self) -> tuple[date, date]:
        return self.first, self.last


def parse(value: str | None, *, default: date) -> Period:
    """Parse ``YYYY-MM`` or ``YYYY``; fall back to the month in ``default``.

    ``default`` is supplied by the caller — the latest month with data — rather
    than read from the clock here, so every surface resolves "no period given"
    the same way.
    """
    if value is None or not str(value).strip():
        return month_period(default)

    text = str(value).strip()

    match = _MONTH.match(text)
    if match:
        year, month = int(match.group(1)), int(match.group(2))
        if not (_MIN_YEAR <= year <= _MAX_YEAR) or not (1 <= month <= 12):
            raise PeriodError("period is outside the supported range")
        return month_period(date(year, month, 1))

    match = _YEAR.match(text)
    if match:
        year = int(match.group(1))
        if not (_MIN_YEAR <= year <= _MAX_YEAR):
            raise PeriodError("period is outside the supported range")
        return Period(
            first=date(year, 1, 1),
            last=date(year, 12, 31),
            label=str(year),
            is_month=False,
        )

    raise PeriodError("period must be YYYY-MM or YYYY")


def parse_month(value: str | None, *, default: date) -> date:
    """A period that must be a single month."""
    period = parse(value, default=default)
    if not period.is_month:
        raise PeriodError("this tool needs a single month, as YYYY-MM")
    return period.first


def month_period(month: date) -> Period:
    first = month.replace(day=1)
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    return Period(first=first, last=last, label=month_label(first), is_month=True)


def month_label(month: date) -> str:
    return f"{calendar.month_name[month.month]} {month.year}"
