"""Building a payload that is safe by construction, then proving it.

Order matters and is not negotiable:

1. **Decide eligibility** — whether this transaction's merchant name may be
   sent at all, from the payment rail rather than from the caller's opinion.
2. **Allow-list** — copy only permitted fields into :class:`AIPayload`, which
   has nowhere to put anything else.
3. **Re-scan the finished payload.** Any detector hit aborts the call.

Step 3 is the one that earns its keep. Steps 1 and 2 are what we *intend*; step
3 checks what we actually built, and it is the only step that survives a bug in
the other two. It fails closed: a payload that trips a detector is not cleaned
up and retried, because a builder that produced dirty output once cannot be
trusted to produce clean output on a second attempt. The transaction goes to
human review instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.privacy import detectors
from app.privacy.allowlist import AIPayload, bucket_amount

_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

#: Rails on which an unmatched merchant name is still a *business* name.
#:
#: A card transaction is settled through an acquirer, which prints the
#: registered merchant's name — you cannot swipe a card at a person. NACH and
#: ACH mandates are the same argument from a different direction: a direct-debit
#: mandate is registered with NPCI by a corporate, and there is no mechanism by
#: which an individual becomes the beneficiary of one. A name on those rails is
#: a lender, an insurer or a utility.
#:
#: Public because the assistant applies the same classification. The rails are
#: a fact about how money moves in India, not a policy belonging to one module,
#: and two copies of it would eventually disagree.
MERCHANT_RAILS = frozenset({"card", "nach", "ach"})
_MERCHANT_RAILS = MERCHANT_RAILS

#: Rails where the counterparty is a person often enough that an unmatched name
#: must never leave the system. Named explicitly rather than inferred by
#: exclusion, so adding a rail is a decision rather than an accident.
_PERSON_RAILS = frozenset({"imps", "neft", "rtgs", "cheque", "cash", "unknown"})

#: UPI is neither: it carries both merchant payments and payments to people.
#: Only an explicit P2M marker makes an unmatched UPI name eligible.
_P2M_MARKERS = ("/P2M/", "-P2M-", " P2M ")


@dataclass(frozen=True, slots=True)
class ScrubResult:
    """Either a payload, or the reason there isn't one."""

    payload: AIPayload | None
    #: Detector name when a re-scan failed, e.g. "PAN". None on success.
    blocked_by: str | None = None
    #: Which field the detector fired on.
    blocked_field: str | None = None

    @property
    def ok(self) -> bool:
        return self.payload is not None


def merchant_is_sendable(
    *, merchant: str | None, is_known: bool, payment_method: str, description: str
) -> bool:
    """May this merchant name cross the perimeter?

    Decided here, from the rail, rather than accepted from a caller — the
    caller is the code most likely to be wrong about it.
    """
    if not merchant:
        return False
    if is_known:
        # Matched in the seeded dictionary: this is a business, by definition.
        return True

    method = str(payment_method).lower()
    if method in _PERSON_RAILS:
        return False
    if method in _MERCHANT_RAILS:
        return True
    if method == "upi":
        upper = description.upper()
        return any(marker in upper for marker in _P2M_MARKERS)
    return False


def build_payload(
    *,
    merchant: str | None,
    merchant_is_known: bool,
    description: str,
    amount: Decimal,
    direction: str,
    payment_method: str,
    mcc: str | None = None,
    txn_date: date | None = None,
) -> ScrubResult:
    """Assemble and verify the only thing a model will see."""
    sendable = merchant_is_sendable(
        merchant=merchant,
        is_known=merchant_is_known,
        payment_method=payment_method,
        description=description,
    )

    try:
        payload = AIPayload(
            merchant=merchant if sendable else None,
            amount_bucket=bucket_amount(amount),
            direction=str(direction),
            payment_method=str(payment_method),
            mcc_hint=mcc,
            day_of_week=_WEEKDAYS[txn_date.weekday()] if txn_date else None,
        )
    except Exception:
        # A payload that will not validate is a payload we do not send. The
        # transaction falls through to review rather than to a looser retry.
        return ScrubResult(payload=None, blocked_by="schema_violation")

    if payload.merchant is None:
        # Nothing identifiable left to ask about. Calling the model with only a
        # bucket and a rail would spend money to be told "Other".
        return ScrubResult(payload=None, blocked_by="no_sendable_merchant")

    # --- the re-scan --------------------------------------------------------
    # Everything above is intent. This is verification, and it is the step that
    # holds when the steps above are wrong.
    for field, value in payload.as_prompt_fields().items():
        if not isinstance(value, str):
            continue
        found = detectors.scan(value)
        if found:
            return ScrubResult(
                payload=None, blocked_by=str(found[0].kind), blocked_field=field
            )

    return ScrubResult(payload=payload)
