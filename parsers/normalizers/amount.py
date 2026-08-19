"""Indian statement amount parsing.

Indian banks print money in the lakh–crore grouping — ``1,23,456.78``, not
``123,456.78`` — so the naive ``value.replace(",", "")`` that works for Western
formats is right here only by accident, and the accident is worth being explicit
about: it works because we discard the separators rather than interpret them.

The genuinely dangerous cases are the ones where sign is carried outside the
number: a trailing ``Dr``/``Cr``, a trailing minus, or accounting parentheses. A
parser that ignores those reads a ₹50,000 credit as a ₹50,000 debit, which
reconciles to a ₹100,000 error and lands in someone's spending total.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from app.models.enums import Direction

# Everything that is not a digit, separator or sign marker.
_CURRENCY_NOISE = re.compile(r"[₹$]|(?<![A-Za-z])(?:rs|inr)\.?(?![A-Za-z])", re.IGNORECASE)
_WHITESPACE = re.compile(r"[\s  ]+")

# `Dr`/`Cr` as a standalone token, optionally dotted. Anchored to a word
# boundary so "CREDIT CARD" in a description is never mistaken for a Cr marker.
_DR_MARKER = re.compile(r"\b(?:d\.?r\.?|dr|debit)\b\.?$", re.IGNORECASE)
_CR_MARKER = re.compile(r"\b(?:c\.?r\.?|cr|credit)\b\.?$", re.IGNORECASE)

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d{1,2})?")


class AmountParseError(ValueError):
    """Raised when a cell cannot be read as money.

    Carries no value text: these propagate into warnings and logs, which are
    forbidden from carrying statement content.
    """


def looks_like_amount(raw: str | None) -> bool:
    """Cheap test used for column detection, not for parsing."""
    if not raw:
        return False
    stripped = _WHITESPACE.sub("", raw)
    if not stripped:
        return False
    return bool(_NUMBER.fullmatch(_strip_markers(stripped)[0].strip("()+-")))


def _strip_markers(text: str) -> tuple[str, int]:
    """Remove sign markers, returning the remainder and a sign (+1/-1)."""
    sign = 1
    working = text.strip()

    if working.startswith("(") and working.endswith(")"):
        # Accounting negative. Common on card statements for refunds.
        sign = -1
        working = working[1:-1].strip()

    if _CR_MARKER.search(working):
        working = _CR_MARKER.sub("", working).strip()
    elif _DR_MARKER.search(working):
        working = _DR_MARKER.sub("", working).strip()

    if working.endswith("-"):
        sign = -sign
        working = working[:-1].strip()
    elif working.startswith("-"):
        sign = -sign
        working = working[1:].strip()
    elif working.startswith("+"):
        working = working[1:].strip()

    return working, sign


def parse_amount(raw: str | None) -> Decimal:
    """Parse a money cell into a positive-or-negative exact Decimal.

    Returns the *signed* value as printed. Callers that need the canonical
    positive-amount-plus-direction form use :func:`parse_amount_with_direction`.
    """
    if raw is None:
        raise AmountParseError("empty amount cell")

    text = _CURRENCY_NOISE.sub("", str(raw))
    text = _WHITESPACE.sub(" ", text).strip()
    if not text or text in {"-", "--", "—", "N/A", "NIL"}:
        raise AmountParseError("empty amount cell")

    body, sign = _strip_markers(text)
    body = body.replace(",", "").replace(" ", "")

    if not body:
        raise AmountParseError("no digits in amount cell")

    try:
        value = Decimal(body)
    except InvalidOperation as exc:
        raise AmountParseError("unparseable amount cell") from exc

    return (value * sign).quantize(Decimal("0.01"))


def parse_amount_with_direction(
    raw: str | None,
    *,
    default: Direction | None = None,
) -> tuple[Decimal, Direction]:
    """Parse a single-column amount that carries its own direction.

    Used by layouts with one Amount column plus a ``Dr``/``Cr`` suffix or a
    leading minus, rather than separate Withdrawal/Deposit columns.

    ``default`` applies only when the cell carries no direction signal at all.
    Passing ``None`` there makes an unmarked cell an error, which is the right
    default for a layout where direction is supposed to be present — guessing
    would silently flip half the statement.
    """
    if raw is None:
        raise AmountParseError("empty amount cell")

    text = _CURRENCY_NOISE.sub("", str(raw))
    text = _WHITESPACE.sub(" ", text).strip()

    explicit: Direction | None = None
    if _CR_MARKER.search(text):
        explicit = Direction.CREDIT
    elif _DR_MARKER.search(text):
        explicit = Direction.DEBIT

    value = parse_amount(raw)

    if explicit is not None:
        return abs(value), explicit
    if value < 0:
        return abs(value), Direction.DEBIT
    if value > 0 and default is not None:
        return value, default
    if default is not None:
        return abs(value), default

    raise AmountParseError("amount cell carries no direction and no default was given")
