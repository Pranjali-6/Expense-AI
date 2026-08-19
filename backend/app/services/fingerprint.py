"""Transaction identity, and therefore duplicate detection.

A fingerprint answers one question: *have we seen this transaction before?* It
is computed over

    tenant_id | account_id | txn_date | amount | direction | normalized text

and the database enforces ``UNIQUE (tenant_id, account_id, fingerprint)``, so a
duplicate is refused by PostgreSQL rather than by whichever code path
remembered to check.

**The running balance is deliberately not an input.** The same transaction
legitimately carries different balances across re-issued, corrected or
overlapping statements — a bank re-issues a statement after a late-posting
correction and every subsequent balance shifts. Folding balance into the key
would produce a different hash for an identical transaction and silently defeat
deduplication, which is precisely the failure this ledger must not have. Balance
is corroborating evidence instead: it breaks ties and feeds
``confidence_validation``, but it never decides identity.

**Genuine repeats are not duplicates.** Two ₹200 Swiggy orders on the same day
are two transactions, and a naive fingerprint would let the database reject the
second one — losing real money from the ledger to "protect" it from a duplicate.
So identical rows *within one statement* are numbered by occurrence, which keeps
them distinct while remaining perfectly stable: the same statement re-uploaded
produces the same rows in the same order, therefore the same occurrence numbers,
therefore the same fingerprints, therefore zero new rows.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections import defaultdict
from datetime import date
from decimal import Decimal

from app.models.enums import Direction

from parsers.canonical import CanonicalTransaction
from parsers.normalizers import text as textnorm

_NON_ALNUM = re.compile(r"[^A-Z0-9 ]+")
_WS = re.compile(r"\s+")


def identity_text(transaction: CanonicalTransaction) -> str:
    """The textual half of the fingerprint.

    Prefers the normalized merchant when the dictionary recognised one, because
    that is stable across banks and across narration formats: the same Swiggy
    order reads as ``BUNDL TECHNOLOGIES`` on one statement and
    ``UPI-SWIGGY-swiggy@ybl`` on another, and both must fingerprint the same.

    Falls back to the description with machine noise removed — reference
    numbers, masked cards, IFSC codes — because those genuinely differ between
    two printings of one transaction.
    """
    if transaction.merchant_slug:
        return transaction.merchant_slug.upper()

    cleaned = textnorm.strip_machine_noise(transaction.description).upper()
    cleaned = _WS.sub(" ", _NON_ALNUM.sub(" ", cleaned)).strip()
    return cleaned or "UNKNOWN"


def compute(
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    txn_date: date,
    amount: Decimal,
    direction: Direction | str,
    text: str,
    occurrence: int = 0,
) -> str:
    """The fingerprint itself.

    Amount is stringified from an exact ``Decimal`` at two places. A float here
    would make identity depend on binary rounding, so ₹1234.56 could fingerprint
    two different ways on two machines.
    """
    payload = "|".join(
        (
            str(tenant_id),
            str(account_id),
            txn_date.isoformat(),
            f"{Decimal(amount).quantize(Decimal('0.01')):f}",
            str(direction),
            text,
            str(occurrence),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def assign(
    transactions: list[CanonicalTransaction],
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> list[str]:
    """Fingerprint a statement's transactions in order.

    Returns fingerprints positionally. Occurrence numbering is assigned in
    statement order, which is what makes a re-upload reproduce them exactly.
    """
    seen: dict[tuple, int] = defaultdict(int)
    fingerprints: list[str] = []

    for transaction in transactions:
        text = identity_text(transaction)
        key = (transaction.txn_date, transaction.amount, str(transaction.direction), text)
        occurrence = seen[key]
        seen[key] += 1

        fingerprints.append(
            compute(
                tenant_id=tenant_id,
                account_id=account_id,
                txn_date=transaction.txn_date,
                amount=transaction.amount,
                direction=transaction.direction,
                text=text,
                occurrence=occurrence,
            )
        )

    return fingerprints


# --------------------------------------------------------------------------- #
# Near-duplicates
# --------------------------------------------------------------------------- #

#: Description similarity above which two same-day, same-amount rows on the same
#: account are treated as probably the same transaction.
SIMILARITY_FLOOR = 0.92


def looks_like_near_duplicate(
    candidate: CanonicalTransaction, existing_description: str
) -> tuple[bool, float]:
    """Same date, amount and direction, with narration that drifted.

    Deliberately **not** a reason to drop the row. A near-duplicate is a
    judgement, and a wrong judgement in either direction costs real money: drop
    a genuine transaction and the ledger understates spending with no trace;
    keep a true duplicate and it overstates. So the row is written, flagged, and
    surfaced in the Review Center for a person to settle — the one actor who can
    actually tell.
    """
    from rapidfuzz import fuzz

    score = fuzz.token_set_ratio(
        candidate.description.upper(), existing_description.upper()
    ) / 100.0
    return score >= SIMILARITY_FLOOR, round(score, 4)
