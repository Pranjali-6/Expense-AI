"""Every number in an answer must have come from a tool result.

This is the check that makes the assistant safe to believe. Everything else in
the design keeps the model away from data it should not see; this keeps it away
from arithmetic it should not do.

The failure it exists to catch is specific and it is not exotic. A model given
``{"food_rupees": 24010, "grocery_rupees": 18300}`` and asked "how much on food
and groceries?" will happily reply "about ₹42,000" — fluent, helpful, and a
number that exists nowhere in this system. Sometimes it will be right. The
problem is that nothing downstream can tell which time it is.

So: extract every figure from the answer, extract every figure available to the
model, and require the first set to be a subset of the second. Not "close to" —
present. A figure that is not present was invented, derived or misremembered,
and all three are the same failure from the user's side of the screen.

**A failed check discards the prose.** It does not annotate it, footnote it or
show it with a warning, because a warning next to a confident sentence is not
something people read. The question is answered from the tool results instead,
by the deterministic renderer that produced them — so the cost of a failure is
plainer phrasing, never a worse answer.

**Figures are matched by kind, not by magnitude.** A rupee amount is checked
against the rupee amounts available, a percentage against the percentages, and
only bare numbers fall back to the union. One flat set of numbers was the first
version of this and it was too weak to be worth much: a view containing
``share_percent: 40`` licensed the sentence "you spent ₹40", and a view for
March licensed "three subscriptions" because ``2024-03`` contains a 3. Field
names carry the kind — ``*_rupees``, ``*_percent`` — so the split costs nothing
and closes the two cases that matter.

Three deliberate looseness decisions, stated rather than buried:

* **Years 1900–2100 pass unchecked as bare numbers.** A year is not a quantity
  and appears in phrasing the extractor cannot always trace ("the 2025
  financial year"). It is not an exemption for money: ``₹2,024`` is checked
  against the rupee figures like any other amount, and fails unless one of them
  is 2024.
* **Word numbers below "two" are not checked.** "one" is a pronoun in English
  far more often than it is a count, and checking it would reject correct
  answers for containing the word "one". Two through twenty are checked.
* **Bare numbers are checked against every kind at once, and dates contribute
  to that pool.** So a wrong *count* can slip through when it happens to equal
  a month number or a day. That is the residual gap, and it is the cheap end of
  the harm: a miscounted transaction is wrong, an invented rupee figure is the
  failure this system exists to prevent, and closing the first would reject
  correct answers for saying "on the 14th".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Final

#: Currency first, then percentages, then bare figures — the alternation order
#: is what stops "₹45" being counted a second time as the bare number 45.
_TOKEN: Final = re.compile(
    r"(?P<currency>(?:₹|\bRs\.?|\bINR)\s*(?P<cur>\d[\d,]*(?:\.\d+)?))"
    r"|(?P<percent>(?P<pct>\d[\d,]*(?:\.\d+)?)\s*(?:%|per\s?cent))"
    r"|(?P<bare>(?<![\d,.\w])(?P<num>\d[\d,]*(?:\.\d+)?))",
    re.IGNORECASE,
)

_WORD_NUMBERS: Final[dict[str, int]] = {
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
}
_WORD_RE: Final = re.compile(
    r"\b(" + "|".join(_WORD_NUMBERS) + r")\b", re.IGNORECASE
)

_YEAR_FLOOR = Decimal("1900")
_YEAR_CEILING = Decimal("2100")


@dataclass(frozen=True, slots=True)
class Figures:
    """Everything the model could legitimately quote, split by kind."""

    currency: set[Decimal] = field(default_factory=set)
    percent: set[Decimal] = field(default_factory=set)
    plain: set[Decimal] = field(default_factory=set)

    @property
    def any_kind(self) -> set[Decimal]:
        return self.currency | self.percent | self.plain


@dataclass(frozen=True, slots=True)
class Untraceable:
    """A figure with no source. ``kind`` is safe to log; ``text`` is not."""

    kind: str
    text: str


@dataclass(frozen=True, slots=True)
class TraceOutcome:
    findings: tuple[Untraceable, ...]

    @property
    def ok(self) -> bool:
        return not self.findings

    @property
    def kinds(self) -> tuple[str, ...]:
        """What went wrong, without saying what the figures were."""
        return tuple(sorted({finding.kind for finding in self.findings}))


def _as_decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None


#: Field-name suffixes that declare a value's kind. The tool views are written
#: to use them, which is what makes the kind-aware check possible at all.
_CURRENCY_SUFFIX = "_rupees"
_PERCENT_SUFFIX = "_percent"


def _kind_of(key: str | None) -> str:
    if key is None:
        return "plain"
    if key.endswith(_CURRENCY_SUFFIX):
        return "currency"
    if key.endswith(_PERCENT_SUFFIX):
        return "percent"
    return "plain"


def allowed_figures(*sources: Any) -> Figures:
    """Every number the model could legitimately quote, split by kind.

    Walks tool results and their arguments, taking numeric values under the
    kind their field name declares and pulling figures out of any string it
    finds. Figures inside a string are classified by the same rules used on the
    answer — ``₹3,692.20`` in a reason sentence is a rupee figure wherever it
    appears — so an engine-authored explanation stays quotable.
    """
    currency: set[Decimal] = set()
    percent: set[Decimal] = set()
    plain: set[Decimal] = set()
    buckets = {"currency": currency, "percent": percent, "plain": plain}

    stack: list[tuple[Any, str]] = [(source, "plain") for source in sources]

    while stack:
        item, kind = stack.pop()
        if isinstance(item, bool):
            # `True` is `1` in Python, and would silently license the figure 1.
            continue
        if isinstance(item, (int, float, Decimal)):
            buckets[kind].add(Decimal(str(item)))
        elif isinstance(item, str):
            for match in _TOKEN.finditer(item):
                if match.group("currency"):
                    value, target = _as_decimal(match.group("cur")), currency
                elif match.group("percent"):
                    value, target = _as_decimal(match.group("pct")), percent
                else:
                    value, target = _as_decimal(match.group("num")), buckets[kind]
                if value is not None:
                    target.add(value)
        elif isinstance(item, dict):
            for key, value in item.items():
                stack.append((value, _kind_of(str(key))))
        elif isinstance(item, (list, tuple)):
            stack.extend((element, kind) for element in item)

    return Figures(currency=currency, percent=percent, plain=plain)


def _is_year(value: Decimal) -> bool:
    return value == value.to_integral_value() and _YEAR_FLOOR <= value <= _YEAR_CEILING


def check(answer: str, allowed: Figures) -> TraceOutcome:
    """Is every figure in ``answer`` present in ``allowed``, in its own kind?"""
    findings: list[Untraceable] = []
    anywhere = allowed.any_kind

    for match in _TOKEN.finditer(answer or ""):
        if match.group("currency"):
            kind, raw, permitted = "currency", match.group("cur"), allowed.currency
        elif match.group("percent"):
            kind, raw, permitted = "percentage", match.group("pct"), allowed.percent
        else:
            kind, raw, permitted = "figure", match.group("num"), anywhere

        value = _as_decimal(raw)
        if value is None:
            findings.append(Untraceable(kind, match.group(0)))
            continue
        if value in permitted:
            continue
        # A bare year is phrasing, not a quantity. A year written as a rupee
        # amount is not exempt — it matched the currency rule, not this one.
        if kind == "figure" and _is_year(value):
            continue
        findings.append(Untraceable(kind, match.group(0)))

    for match in _WORD_RE.finditer(answer or ""):
        value = Decimal(_WORD_NUMBERS[match.group(1).lower()])
        if value not in anywhere:
            findings.append(Untraceable("word_number", match.group(0)))

    return TraceOutcome(findings=tuple(findings))
