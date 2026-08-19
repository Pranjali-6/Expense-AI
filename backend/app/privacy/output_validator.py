"""Distrusting the model's answer.

Everything on the way *in* assumes the model might be attacked. This module
assumes it might already have been. A successful injection shows up in what
comes back — an echoed identifier, a URL to fetch, a tool call to run — so the
response is validated independently of how careful the request was.

Four checks, cheapest first:

1. **Schema.** The category must be one of the 22 fixed slugs. Not "close to
   one", not a new one the model invented.
2. **Range.** Confidence must be a real number in [0, 1].
3. **PII echo.** No detector may fire on any returned string. A model that
   returns an account number is a model that was given one, or one that
   hallucinated one — and both are incidents.
4. **Shape.** No URLs, no tool-call structures, no markup. A category name has
   no legitimate reason to contain any of them.

A response failing any check is not repaired. It becomes ``Other`` with the
transaction routed to review, and an incident is recorded. Repairing an
untrusted response means guessing what it meant to say.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from app.privacy import detectors

#: The 22 canonical categories. A closed set: the model chooses from it or the
#: answer is rejected.
VALID_CATEGORIES: Final[frozenset[str]] = frozenset(
    {
        "food", "grocery", "rent", "utilities", "shopping", "travel", "fuel",
        "entertainment", "subscriptions", "healthcare", "insurance", "education",
        "emi", "investment", "salary", "bank_charges", "taxes", "cash_withdrawal",
        "transfers", "credit_card_payment", "refund", "other",
    }
)

_URL = re.compile(r"https?://|www\.|\b\w+\.(?:com|net|org|io|ai|co|in)\b", re.IGNORECASE)
_TOOL_SHAPE = re.compile(
    r'"?(?:function_call|tool_call|tool_use|function|arguments|parameters)"?\s*[:=]'
    r'|<function|<tool|\{\s*"name"\s*:',
    re.IGNORECASE,
)
_MARKUP = re.compile(r"```|<script|javascript:|data:text/html|<\|", re.IGNORECASE)

#: The model is asked for a short justification. Anything longer is either a
#: rambling model or a payload.
_MAX_REASONING = 200


@dataclass(frozen=True, slots=True)
class ValidatedPrediction:
    category_slug: str
    confidence: Decimal
    reasoning: str | None = None


@dataclass(frozen=True, slots=True)
class ValidationOutcome:
    prediction: ValidatedPrediction | None
    #: Why it was rejected — a code, never the offending text.
    rejected_by: str | None = None
    #: Set when a privacy detector fired, so the incident records which one.
    detector: str | None = None

    @property
    def ok(self) -> bool:
        return self.prediction is not None


def _string_values(payload: Any) -> list[str]:
    """Every string anywhere in the response, however nested."""
    found: list[str] = []
    stack: list[Any] = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            found.append(item)
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return found


def validate(raw: Any) -> ValidationOutcome:
    """Check a model response before any of it is believed."""
    if not isinstance(raw, dict):
        return ValidationOutcome(None, rejected_by="not_an_object")

    # Whole-response checks first: they apply to every field, including ones we
    # never read, because a payload hidden in an unexpected key still counts.
    #
    # PII is tested *before* the shape checks, and the order is not cosmetic. An
    # echoed email address matches the URL pattern too — `owner@example.com`
    # contains `example.com` — so testing shapes first would file a genuine
    # privacy leak as "a URL appeared", raising a schema incident instead of a
    # PII incident in the exact report built to surface leaks. Both reject the
    # response; only one of them tells the truth about why.
    values = _string_values(raw)

    for value in values:
        found = detectors.scan(value)
        if found:
            return ValidationOutcome(
                None, rejected_by="pii_echo", detector=str(found[0].kind)
            )

    for value in values:
        if _TOOL_SHAPE.search(value):
            return ValidationOutcome(None, rejected_by="tool_call_shape")
        if _URL.search(value):
            return ValidationOutcome(None, rejected_by="url_in_output")
        if _MARKUP.search(value):
            return ValidationOutcome(None, rejected_by="markup_in_output")

    category = raw.get("category")
    if not isinstance(category, str):
        return ValidationOutcome(None, rejected_by="missing_category")

    slug = category.strip().lower().replace(" ", "_").replace("-", "_")
    if slug not in VALID_CATEGORIES:
        # Deliberately no fuzzy matching. "food_delivery" is not a category, and
        # guessing which one the model meant is how an unbounded output becomes
        # a bounded one that is silently wrong.
        return ValidationOutcome(None, rejected_by="unknown_category")

    raw_confidence = raw.get("confidence", 0)
    try:
        confidence = Decimal(str(raw_confidence))
    except (InvalidOperation, TypeError, ValueError):
        return ValidationOutcome(None, rejected_by="unparseable_confidence")
    if not (Decimal("0") <= confidence <= Decimal("1")):
        return ValidationOutcome(None, rejected_by="confidence_out_of_range")

    reasoning = raw.get("reasoning")
    if reasoning is not None:
        if not isinstance(reasoning, str):
            return ValidationOutcome(None, rejected_by="malformed_reasoning")
        reasoning = reasoning.strip()[:_MAX_REASONING] or None

    return ValidationOutcome(
        prediction=ValidatedPrediction(
            category_slug=slug,
            confidence=confidence.quantize(Decimal("0.0001")),
            reasoning=reasoning,
        )
    )


# --------------------------------------------------------------------------- #
# Free prose (the assistant and the monthly narrative)
# --------------------------------------------------------------------------- #

#: An assistant answer is two or three sentences. Anything much longer is a
#: model that has stopped answering and started padding, or a payload.
_MAX_PROSE = 1200

#: Rupee figures are lifted out before the PII scan runs.
#:
#: This looks like a hole and is the opposite of one. A rupee figure is a run of
#: digits, and the account-number and long-digit-run detectors exist to catch
#: exactly that shape — so scanning an answer about money with them intact
#: rejects the answer for containing the amount it was asked about. The figures
#: are not unchecked: every one of them must appear in a tool result, which the
#: traceability check enforces before this function is reached. A digit run that
#: is *not* a rupee figure still meets the full detector chain here.
_CURRENCY_SPAN: Final = re.compile(
    r"(?:₹|\bRs\.?|\bINR)\s*\d[\d,]*(?:\.\d+)?", re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class ProseOutcome:
    text: str | None
    #: A code, never the offending text.
    rejected_by: str | None = None
    detector: str | None = None

    @property
    def ok(self) -> bool:
        return self.text is not None


def validate_prose(raw: Any) -> ProseOutcome:
    """Check a generated sentence before a person is shown it.

    Same distrust as :func:`validate`, applied to text rather than a schema: a
    successful injection surfaces in what comes back, so what comes back is
    checked on its own terms rather than on the assumption that the request was
    careful.
    """
    if not isinstance(raw, str):
        return ProseOutcome(None, rejected_by="not_text")

    text = raw.strip()
    if not text:
        return ProseOutcome(None, rejected_by="empty")
    if len(text) > _MAX_PROSE:
        return ProseOutcome(None, rejected_by="oversized")

    scannable = _CURRENCY_SPAN.sub(" ", text)
    found = detectors.scan(scannable)
    if found:
        return ProseOutcome(None, rejected_by="pii_echo", detector=str(found[0].kind))

    if _TOOL_SHAPE.search(text):
        return ProseOutcome(None, rejected_by="tool_call_shape")
    if _URL.search(text):
        return ProseOutcome(None, rejected_by="url_in_output")
    if _MARKUP.search(text):
        return ProseOutcome(None, rejected_by="markup_in_output")

    return ProseOutcome(text=text)
