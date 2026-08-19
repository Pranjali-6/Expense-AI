"""The complete set of things a language model may ever see.

This module is the perimeter. Everything else in ``app/privacy`` supports it,
but the guarantee lives here and it is **structural rather than procedural**:
``AIPayload`` is a Pydantic model with ``extra="forbid"`` and seven fields, none
of which is an account number, a card number, a UPI ID, a PAN, a name, an
address, a phone number, an email, a statement number, or an exact amount.

That matters more than it sounds. A validating filter can be bypassed by a code
path that forgets to call it; a model with no field for an account number makes
the account number *unrepresentable*. There is no argument you can pass to send
one. The worst a careless caller can do is put an account number in a field
meant for something else — which is what the scrubber and the post-build
re-scan exist to catch, and both fail closed.

**Amounts are bucketed, never exact.** An exact rupee value adds almost nothing
to a categorisation decision — ₹487.50 and ₹492.00 at the same merchant are the
same kind of purchase — while being one of the strongest re-identification
signals a transaction carries. A bucket keeps the signal and drops the
fingerprint.

**There is no description field, and that is a deliberate removal.** An earlier
version carried a "description hint" — letters only, digits stripped, six words
— on the theory that some merchants are identifiable only from narration
wording. It defeated the entire perimeter on the first test: for
``IMPS-412312345678-RAHUL SHARMA-HDFC-XXXXXX1234`` the merchant was correctly
withheld as unverified, and the hint then sent ``Rahul Sharma`` anyway. No
filter fixes that, because nothing about the shape of a name distinguishes
"Rahul Sharma" from "Rahul Sweets". The field is gone.

**Which merchant names may be sent is decided by the payment rail, not by the
caller.** Three cases:

* a name matched in the seeded merchant dictionary is a business, and is sent;
* an *unmatched* name on a card rail is still a business — a card swipe happens
  at a registered merchant, and the acquirer prints that merchant's name — so it
  is sent, which is what lets the model categorise the shops the dictionary has
  never heard of;
* an unmatched name on any transfer rail (IMPS, NEFT, RTGS) is withheld, because
  that is where a counterparty is a person. Those transactions skip AI entirely
  and go to review, which is the right destination for an unidentified payee.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: The allow-list, as data. The Privacy Center renders this from the model's
#: own fields rather than from a hardcoded copy, so the screen cannot claim a
#: narrower perimeter than the code enforces.
ALLOWED_FIELDS: Final[tuple[str, ...]] = (
    "merchant",
    "amount_bucket",
    "direction",
    "payment_method",
    "mcc_hint",
    "day_of_week",
)


class AmountBucket(StrEnum):
    """Coarse magnitude. Deliberately not a number.

    Boundaries are chosen around how Indian household spending actually falls —
    a ₹200 purchase and a ₹2,00,000 one are different kinds of event, while
    ₹487 and ₹492 are not.
    """

    UNDER_100 = "under_100"
    R100_500 = "100_500"
    R500_1K = "500_1000"
    R1K_5K = "1000_5000"
    R5K_10K = "5000_10000"
    R10K_50K = "10000_50000"
    R50K_1L = "50000_100000"
    OVER_1L = "over_100000"


_BUCKET_EDGES: Final[tuple[tuple[Decimal, AmountBucket], ...]] = (
    (Decimal("100"), AmountBucket.UNDER_100),
    (Decimal("500"), AmountBucket.R100_500),
    (Decimal("1000"), AmountBucket.R500_1K),
    (Decimal("5000"), AmountBucket.R1K_5K),
    (Decimal("10000"), AmountBucket.R5K_10K),
    (Decimal("50000"), AmountBucket.R10K_50K),
    (Decimal("100000"), AmountBucket.R50K_1L),
)


def bucket_amount(amount: Decimal | str | int) -> AmountBucket:
    """Map an exact amount onto a bucket. One-way, on purpose."""
    if isinstance(amount, float):
        raise TypeError("amount must be Decimal or str; float is never money here")
    value = abs(Decimal(str(amount)))
    for edge, bucket in _BUCKET_EDGES:
        if value < edge:
            return bucket
    return AmountBucket.OVER_1L


#: Payment rails are safe to send: they describe the mechanism, not the person.
#: Enumerated rather than free text so a caller cannot smuggle a string through.
_ALLOWED_METHODS: Final[frozenset[str]] = frozenset(
    {
        "upi", "neft", "imps", "rtgs", "card", "atm", "cheque", "ach", "nach",
        "cash", "netbanking", "internal", "unknown",
    }
)

_ALLOWED_DIRECTIONS: Final[frozenset[str]] = frozenset({"debit", "credit"})

_ALLOWED_DAYS: Final[frozenset[str]] = frozenset(
    {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
)


class AIPayload(BaseModel):
    """Everything a model may see about one transaction. Nothing else exists.

    ``extra="forbid"`` is the load-bearing line: an attempt to add a field —
    by a future contributor, by a merge, by a dict spread — fails at
    construction rather than silently widening the perimeter.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_max_length=120)

    #: Dictionary-verified merchant name only. See ``from_transaction``.
    merchant: str | None = Field(default=None, max_length=80)

    amount_bucket: AmountBucket
    direction: str
    payment_method: str = "unknown"

    #: Merchant category code from the dictionary — a four-digit industry code,
    #: not an identifier of anyone.
    mcc_hint: str | None = Field(default=None, max_length=4)

    #: Weekday only. The full date is a re-identification signal and adds
    #: nothing a weekday does not.
    day_of_week: str | None = None

    @field_validator("direction")
    @classmethod
    def _known_direction(cls, value: str) -> str:
        if value not in _ALLOWED_DIRECTIONS:
            raise ValueError("direction must be debit or credit")
        return value

    @field_validator("payment_method")
    @classmethod
    def _known_method(cls, value: str) -> str:
        if value not in _ALLOWED_METHODS:
            raise ValueError("payment_method is not in the allow-list")
        return value

    @field_validator("day_of_week")
    @classmethod
    def _known_day(cls, value: str | None) -> str | None:
        if value is not None and value not in _ALLOWED_DAYS:
            raise ValueError("day_of_week must be a three-letter weekday")
        return value

    @field_validator("mcc_hint")
    @classmethod
    def _numeric_mcc(cls, value: str | None) -> str | None:
        if value is not None and not (value.isdigit() and len(value) == 4):
            raise ValueError("mcc_hint must be a four-digit code")
        return value

    def as_prompt_fields(self) -> dict[str, Any]:
        """The payload as it will be rendered, omitting absent fields."""
        return {
            key: (value.value if isinstance(value, StrEnum) else value)
            for key, value in self.model_dump().items()
            if value is not None
        }

    def field_names(self) -> list[str]:
        """Which fields are actually being sent — recorded per call."""
        return sorted(self.as_prompt_fields())
