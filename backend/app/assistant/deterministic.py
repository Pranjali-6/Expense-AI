"""Answering without a model.

``AI_ENABLED`` is false by default and the product is required to be whole in
that state, so "the assistant" cannot mean "the model". What the model actually
contributes is *phrasing*: it reads a question, picks a tool, and turns the
result into a sentence. Two of those three are things ordinary code does well.

So this module does them. It maps a question onto one of the seven tools with a
small, explicit rule cascade, and every tool already carries a deterministic
sentence describing its own result. With no key configured the assistant still
answers the questions below; with a key it answers free-form ones too, and this
same code is what the answer falls back to when the traceability check rejects
what the model wrote.

The rules are deliberately shallow and deliberately refuse. There is no fuzzy
scoring, no embedding, no "closest match" — a question this cascade does not
recognise gets "I could not tell what you meant, here is what I can answer",
which is a worse experience than a clever guess exactly once, and a better one
every time the clever guess would have been wrong about someone's money.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.intelligence import analytics

#: The canned questions. One per tool, so the offered set demonstrably covers
#: everything the assistant can do rather than sampling it.
@dataclass(frozen=True, slots=True)
class Suggestion:
    id: str
    question: str
    tool: str


SUGGESTIONS: tuple[Suggestion, ...] = (
    Suggestion("month_summary", "How much did I spend this month?", "get_monthly_spending"),
    Suggestion("by_category", "What did I spend it on?", "get_category_spending"),
    Suggestion("top_merchants", "Where did most of my money go?", "get_top_merchants"),
    Suggestion("subscriptions", "What subscriptions do I have?", "get_recurring_expenses"),
    Suggestion("month_change", "Why did my spending change?", "compare_months"),
    Suggestion("large", "Show me transactions above ₹10,000", "get_transactions"),
    Suggestion("unusual", "Is anything unusual?", "get_anomalies"),
)

BY_ID = {suggestion.id: suggestion for suggestion in SUGGESTIONS}


@dataclass(frozen=True, slots=True)
class Plan:
    """Which tool to run, with which arguments."""

    tool: str
    arguments: dict[str, Any]


# --------------------------------------------------------------------------- #
# Reading a period out of the question
# --------------------------------------------------------------------------- #

_MONTH_NAMES = {
    name.lower(): number
    for number, name in enumerate(calendar.month_name)
    if name
} | {
    name.lower(): number
    for number, name in enumerate(calendar.month_abbr)
    if name
}

_EXPLICIT_MONTH = re.compile(r"\b(20\d{2})-(0[1-9]|1[0-2])\b")
_EXPLICIT_YEAR = re.compile(r"\b(20\d{2})\b")
_AMOUNT = re.compile(
    r"(?:above|over|more than|greater than|at least|below|under|less than)\s*"
    r"(?:₹|rs\.?|inr)?\s*([\d,]+(?:\.\d+)?)\s*(k|thousand|lakh|lakhs)?",
    re.IGNORECASE,
)
_LOWER_BOUND = re.compile(r"\b(?:above|over|more than|greater than|at least)\b", re.IGNORECASE)


def _months_in(question: str, *, default: date) -> list[str]:
    """Every month the question names, in the order it names them."""
    found: list[tuple[int, str]] = []

    for match in _EXPLICIT_MONTH.finditer(question):
        found.append((match.start(), f"{match.group(1)}-{match.group(2)}"))

    for match in re.finditer(r"\b([a-z]+)\b(?:\s+(20\d{2}))?", question, re.IGNORECASE):
        number = _MONTH_NAMES.get(match.group(1).lower())
        if number is None:
            continue
        # A bare month name means the most recent occurrence of that month at
        # or before the anchor — "April" in March 2025 is April 2024, not a
        # month that has not happened.
        year = int(match.group(2)) if match.group(2) else default.year
        if not match.group(2) and number > default.month:
            year -= 1
        found.append((match.start(), f"{year}-{number:02d}"))

    found.sort()
    # Dedupe while keeping order: "compare March and March" is one month.
    seen: set[str] = set()
    ordered: list[str] = []
    for _, value in found:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def _period(question: str, *, default: date) -> str | None:
    """The single period the question is about, if it names one."""
    lowered = question.lower()

    match = _EXPLICIT_MONTH.search(question)
    if match:
        return f"{match.group(1)}-{match.group(2)}"

    if "last year" in lowered or "previous year" in lowered:
        return str(default.year - 1)
    if "this year" in lowered or "the year" in lowered:
        return str(default.year)

    months = _months_in(question, default=default)
    if months:
        return months[0]

    year = _EXPLICIT_YEAR.search(question)
    if year:
        return year.group(1)

    if "last month" in lowered or "previous month" in lowered:
        return analytics.previous_month(default).strftime("%Y-%m")

    return None


def _amount_bound(question: str) -> tuple[str, int] | None:
    """A threshold like "above ₹10,000" or "under 500"."""
    match = _AMOUNT.search(question)
    if not match:
        return None
    try:
        value = Decimal(match.group(1).replace(",", ""))
    except Exception:
        return None
    unit = (match.group(2) or "").lower()
    if unit in {"k", "thousand"}:
        value *= 1000
    elif unit in {"lakh", "lakhs"}:
        value *= 100000
    bound = "min_amount" if _LOWER_BOUND.search(match.group(0)) else "max_amount"
    return bound, int(value)


# --------------------------------------------------------------------------- #
# Reading a subject out of the question
# --------------------------------------------------------------------------- #

async def _category_in(session: AsyncSession, question: str) -> str | None:
    """A category the question names, matched against the seeded list.

    Read from the database rather than a hardcoded synonym table: the category
    names the user sees are the names they will type, and keeping one source
    means renaming a category does not quietly break the assistant.
    """
    lowered = f" {question.lower()} "
    rows = (await session.execute(text("SELECT slug, name FROM categories"))).all()
    hits: list[tuple[int, str]] = []
    for row in rows:
        for candidate in {row.slug.replace("_", " "), row.name.lower()}:
            if f" {candidate} " in lowered or lowered.strip().endswith(f" {candidate}"):
                hits.append((len(candidate), row.slug))
    if not hits:
        return None
    # Longest match wins: "credit card payment" must not lose to "card".
    return max(hits)[1]


async def _merchant_in(session: AsyncSession, question: str) -> str | None:
    """A merchant the question names, matched against this ledger's own names.

    Scoped by RLS to the caller's own transactions, so the match set is the
    user's merchants and nobody else's.
    """
    lowered = question.lower()
    rows = (
        await session.execute(
            text(
                "SELECT DISTINCT merchant FROM transactions "
                "WHERE merchant IS NOT NULL AND length(merchant) >= 3"
            )
        )
    ).all()
    hits = [row.merchant for row in rows if row.merchant.lower() in lowered]
    return max(hits, key=len) if hits else None


# --------------------------------------------------------------------------- #
# The cascade
# --------------------------------------------------------------------------- #

_SUBSCRIPTION_WORDS = ("subscription", "recurring", "renew", "monthly charge", "direct debit")
_ANOMALY_WORDS = ("unusual", "strange", "odd", "outlier", "stands out", "stand out",
                  "anything wrong", "suspicious", "spike")
_COMPARE_WORDS = ("compare", "comparison", " vs ", "versus", "why did", "why has",
                  "increase", "increased", "decrease", "decreased", "go up", "went up",
                  "gone up", "went down", "change", "changed", "more than last",
                  "less than last")
_MERCHANT_WORDS = ("top merchant", "which merchant", "where did", "where is my money",
                   "most money", "biggest merchant", "who did i pay", "top shops")
_LIST_WORDS = ("show me", "list", "transactions", "which transactions", "what did i buy")
_SPEND_WORDS = ("how much", "spend", "spent", "spending", "total", "income", "savings",
                "cash flow", "earn")


def _contains(question: str, words: tuple[str, ...]) -> bool:
    lowered = f" {question.lower()} "
    return any(word in lowered for word in words)


async def plan(
    session: AsyncSession, question: str, *, default_month: date
) -> Plan | None:
    """Map a question onto a tool call, or refuse.

    Ordered most specific first. Each rule reads only what it needs, so adding
    one is a local change rather than a re-tuning of the whole cascade.
    """
    if not question or not question.strip():
        return None

    anchor = default_month.strftime("%Y-%m")
    period = _period(question, default=default_month)
    category = await _category_in(session, question)

    # 1. Subscriptions — a distinct question with a distinct tool.
    if _contains(question, _SUBSCRIPTION_WORDS):
        return Plan("get_recurring_expenses", {})

    # 2. Outliers.
    if _contains(question, _ANOMALY_WORDS):
        return Plan("get_anomalies", {"limit": 10})

    # 3. A comparison. Two named months win over the default pair.
    if _contains(question, _COMPARE_WORDS):
        months = _months_in(question, default=default_month)
        if len(months) >= 2:
            left, right = months[0], months[1]
        else:
            left = analytics.previous_month(default_month).strftime("%Y-%m")
            right = anchor
        arguments: dict[str, Any] = {"left": left, "right": right}
        if category:
            arguments["category"] = category
        return Plan("compare_months", arguments)

    # 4. An explicit amount threshold is unambiguous: it is a list question.
    bound = _amount_bound(question)
    if bound:
        key, value = bound
        arguments = {key: value, "period": period or anchor, "limit": 10}
        if category:
            arguments["category"] = category
        return Plan("get_transactions", arguments)

    # 5. A merchant this ledger actually knows.
    merchant = await _merchant_in(session, question)
    if merchant:
        return Plan(
            "get_transactions",
            {"merchant": merchant, "period": period or anchor, "limit": 10},
        )

    # 6. A category.
    if category:
        return Plan(
            "get_category_spending", {"category": category, "period": period or anchor}
        )

    # 7. "Where did my money go".
    if _contains(question, _MERCHANT_WORDS):
        return Plan("get_top_merchants", {"period": period or anchor, "limit": 10})

    # 8. A list of transactions with no other handle on it.
    if _contains(question, _LIST_WORDS):
        return Plan("get_transactions", {"period": period or anchor, "limit": 10})

    # 9. The general spending question.
    if _contains(question, _SPEND_WORDS):
        if period and len(period) == 4:
            return Plan("get_category_spending", {"period": period})
        return Plan("get_monthly_spending", {"month": period or anchor})

    return None


def plan_for_suggestion(suggestion_id: str, *, default_month: date) -> Plan | None:
    """The plan behind a one-tap question.

    Separate from :func:`plan` on purpose. A canned question must run the tool
    it advertises, every time, without going through text matching that could
    route it somewhere else after an unrelated edit to the cascade.
    """
    suggestion = BY_ID.get(suggestion_id)
    if suggestion is None:
        return None

    anchor = default_month.strftime("%Y-%m")
    previous = analytics.previous_month(default_month).strftime("%Y-%m")

    by_tool: dict[str, dict[str, Any]] = {
        "get_monthly_spending": {"month": anchor},
        "get_category_spending": {"period": anchor},
        "get_top_merchants": {"period": anchor, "limit": 10},
        "get_recurring_expenses": {},
        "compare_months": {"left": previous, "right": anchor},
        "get_transactions": {"period": anchor, "min_amount": 10000, "limit": 10},
        "get_anomalies": {"limit": 10},
    }

    return Plan(suggestion.tool, by_tool[suggestion.tool])
