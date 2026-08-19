"""What the assistant's model is allowed to see, and what it never is.

This is a **second, narrower perimeter** than the one in ``app/privacy``, and
the difference is worth stating plainly rather than leaving implicit.

``AIPayload`` (P6) governs categorisation: one transaction, six fields, amounts
as buckets. It is as small as a payload can be while still being useful. The
assistant cannot work inside it — "how much did I spend on food in March" is
unanswerable from a bucket — so this module defines the wider set the assistant
path uses, and the widening is bounded by four rules:

1. **Per-tool field allow-lists.** Every tool declares exactly which keys reach
   the model. The projection is positive, not subtractive: a column added to a
   query later does not silently reach a prompt, because it was never named.
2. **Descriptions never leave.** Raw narration is where account numbers, UPI
   IDs and counterparty names live. No tool result carries one, at any depth.
   The user sees them in the UI; the model does not.
3. **Merchant names by rail, as in P6, only stricter.** A dictionary-verified
   merchant is a business and is sent. An unmatched name on a card rail is a
   business — you cannot swipe a card at a person — and is sent. Everything
   else is withheld and flagged, including unmatched UPI, which P6 forwards
   when the narration carries an explicit P2M marker. Tool results have no
   narration to check, so the marker cannot be verified, so the name does not
   go. Withheld rows keep their amounts: dropping them would make a total
   wrong, and a wrong total is worse than an unnamed payee.
4. **Amounts are whole rupees.** Rounding is not a privacy measure here — the
   user's own totals are the subject — it is a *traceability* measure. The
   model quotes what it was given, the post-check compares like with like, and
   ₹12,458 is easier to verify against a source than ₹12,457.63 rendered five
   different ways.

Then the same thing P6 does: **re-scan what was actually built.** Every string
anywhere in the finished view runs through the detector chain. A hit aborts the
model call and the question is answered deterministically instead. Fail closed,
always — the fallback answer is computed from the same figures, so refusing to
call the model costs phrasing, not correctness.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.privacy import detectors, injection_guard
from app.privacy.scrubber import MERCHANT_RAILS

#: The rail classification is shared with P6 rather than restated. What is
#: narrower here is the *UPI* case: P6 forwards an unmatched UPI name when the
#: narration carries an explicit P2M marker, and a tool result has no narration
#: to check, so it does not.
_MERCHANT_RAILS = MERCHANT_RAILS

#: What the model is told instead of a withheld name.
WITHHELD = "an unnamed payee"


@dataclass(frozen=True, slots=True)
class RedactionOutcome:
    """The finished model view, or the reason there isn't one."""

    view: Any
    #: Detector or guard that fired. None on success.
    blocked_by: str | None = None

    @property
    def ok(self) -> bool:
        return self.blocked_by is None


def rupees(value: Any) -> int:
    """A money value as whole rupees.

    ``ROUND_HALF_UP`` rather than banker's rounding: this number is read by a
    person who will compare it against a statement, and 2.5 → 2 surprises them.
    """
    if value is None:
        return 0
    if isinstance(value, float):
        raise TypeError("money is never a float here")
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def percent(value: Any) -> int:
    """A ratio in [0, 1] as a whole percentage.

    Pre-computed on purpose. If the model had to turn 0.4523 into "45%" it
    would be doing arithmetic, which it is not allowed to do and which the
    traceability check would then have to permit — widening the check into
    something that no longer catches invented figures.
    """
    if value is None:
        return 0
    return int(
        (Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )


async def known_merchants(session: AsyncSession, names: list[str]) -> set[str]:
    """Which of these names are in the seeded merchant dictionary.

    One query for the whole batch. A name that matches a dictionary entry is a
    business by definition — that is what the dictionary is — so this is the
    check that lets Swiggy through and holds a stranger's name back.
    """
    wanted = [name for name in names if name]
    if not wanted:
        return set()
    rows = (
        await session.execute(
            text(
                "SELECT display_name FROM merchants "
                "WHERE lower(display_name) = ANY(:names)"
            ),
            {"names": [name.lower() for name in wanted]},
        )
    ).all()
    matched = {row.display_name.lower() for row in rows}
    return {name for name in wanted if name.lower() in matched}


async def business_merchants(session: AsyncSession, names: list[str]) -> set[str]:
    """Names that are businesses — by the dictionary, or by the rails they use.

    Aggregate tools group a merchant across every payment it ever received, so
    there is no single payment method to reason from. Rather than give up and
    withhold every name the dictionary has not heard of — which withholds most
    real shops — this asks a stricter question of the ledger: did *every*
    payment to this name go over a rail an individual cannot be on?

    ``bool_and`` rather than a modal rail. One IMPS payment to a name means the
    name might be a person, whatever the other twenty payments looked like, and
    the cost of being wrong is somebody's landlord in a prompt.
    """
    wanted = [name for name in names if name]
    if not wanted:
        return set()

    known = await known_merchants(session, wanted)
    remaining = [name for name in wanted if name not in known]
    if not remaining:
        return known

    rows = (
        await session.execute(
            text(
                """
                SELECT t.merchant
                FROM transactions t
                WHERE t.merchant = ANY(:names)
                GROUP BY t.merchant
                HAVING bool_and(t.payment_method = ANY(:rails))
                """
            ),
            {"names": remaining, "rails": sorted(MERCHANT_RAILS)},
        )
    ).all()
    return known | {row.merchant for row in rows}


def merchant_for_model(
    name: str | None, *, is_known: bool, payment_method: str | None = None
) -> str | None:
    """The merchant name as the model may see it, or None if it may not.

    Also runs the injection guard. A merchant name is chosen by whoever sent
    the payment, which makes it the platform's indirect-injection channel: name
    your shop "ignore previous instructions" and it reaches a prompt. A
    quarantined name is withheld rather than cleaned, exactly as in P6 —
    rewriting an attack into something that looks safe is a guess about what
    the attacker meant.
    """
    if not name:
        return None
    if not injection_guard.inspect(name).safe:
        return None
    if is_known:
        return name
    if (payment_method or "").lower() in _MERCHANT_RAILS:
        return name
    return None


def _strings(payload: Any) -> list[str]:
    """Every string anywhere in the view, however nested."""
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


def verify(view: Any) -> RedactionOutcome:
    """Re-scan a finished model view. The step that survives a bug in the rest.

    Everything above this line is intent. This checks what was actually built,
    and it is the only part that still holds when a tool grows a field someone
    forgot to think about.
    """
    for value in _strings(view):
        found = detectors.scan(value)
        if found:
            return RedactionOutcome(view=None, blocked_by=str(found[0].kind))
        if not injection_guard.inspect(value).safe:
            return RedactionOutcome(view=None, blocked_by="injection_in_tool_result")
    return RedactionOutcome(view=view)


def scrub_mentions(sentence: str, *, withheld: list[str]) -> str:
    """Remove withheld merchant names from a generated sentence.

    Anomaly reasons are assembled deterministically from a merchant name and
    two numbers. When the name may not be sent, the sentence still can be —
    with the name replaced. The numbers are the part the model needs.
    """
    result = sentence
    for name in withheld:
        if name:
            result = result.replace(name, WITHHELD)
    return result
