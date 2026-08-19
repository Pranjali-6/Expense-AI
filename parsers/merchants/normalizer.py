"""Merchant identification from Indian statement narrations.

A narration is a machine record that happens to contain a merchant name:

    UPI-SWIGGY-SWIGGY@YBL-YESB0YBLUPI-412345678901-ORDER PAYMENT
    POS 4123XXXXXXXX8842 SWIGGY               BANGALORE IN
    NEFT-CITIN52410318-BUNDL TECHNOLOGIES PVT LTD

All three are Swiggy. Getting them to agree matters more than it looks: the
category cascade, subscription detection, spend-by-merchant analytics and the
user's own rules all key off the normalized name, so three spellings of one
merchant become three unrelated merchants everywhere in the product.

**Dictionary first, fuzzy second, and the difference is recorded.** A match
against the seeded alias table is a fact. A fuzzy match is a guess, and
``is_known`` says which one you have — the privacy gateway in P6 relies on that
flag to refuse to send anything but a dictionary-verified name to a model, so a
narration containing a person's name can never leave the perimeter as a
"merchant".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from app.models.enums import PaymentMethod

from parsers.normalizers import text as textnorm

# --------------------------------------------------------------------------- #
# Payment rail detection
#
# The leading token of an Indian narration names the rail that carried the
# money. It is printed by the bank's core system, not typed by anyone, so it is
# reliable in a way the rest of the string is not.
# --------------------------------------------------------------------------- #

_RAIL_PATTERNS: tuple[tuple[re.Pattern[str], PaymentMethod], ...] = (
    (re.compile(r"^\s*upi\b|^\s*upiar\b|\bupi[/\-]", re.I), PaymentMethod.UPI),
    (re.compile(r"^\s*(?:pos|vps|ecom|ecomm|ips|card)\b", re.I), PaymentMethod.CARD),
    (re.compile(r"^\s*(?:atw|nwd|cwd|atd|atm)\b|\batm\s+(?:cash|wdl|withdrawal)\b", re.I), PaymentMethod.ATM),
    (re.compile(r"^\s*neft\b|\bneft[/\-]", re.I), PaymentMethod.NEFT),
    (re.compile(r"^\s*(?:imps|mmt)\b|\bimps[/\-]", re.I), PaymentMethod.IMPS),
    (re.compile(r"^\s*rtgs\b|\brtgs[/\-]", re.I), PaymentMethod.RTGS),
    (re.compile(r"^\s*(?:chq|cheque|clg|cts|micr)\b", re.I), PaymentMethod.CHEQUE),
    (re.compile(r"^\s*(?:nach|ecs)\b", re.I), PaymentMethod.NACH),
    (re.compile(r"^\s*ach\b", re.I), PaymentMethod.ACH),
    (re.compile(r"^\s*(?:inb|ib|netbanking|bil|billpay|onl)\b", re.I), PaymentMethod.NETBANKING),
    (re.compile(r"^\s*(?:cash|csh)\b|\bcash\s+dep", re.I), PaymentMethod.CASH),
)


def detect_payment_method(description: str) -> PaymentMethod:
    for pattern, method in _RAIL_PATTERNS:
        if pattern.search(description):
            return method
    return PaymentMethod.UNKNOWN


# --------------------------------------------------------------------------- #
# Tokens that are never a merchant
#
# Rail names, bank short codes and the filler words Indian narrations carry.
# Without this list the fuzzy matcher happily decides that "PAYMENT" is a
# merchant and that every UPI transaction was made at the same place.
# --------------------------------------------------------------------------- #

_STOP_TOKENS: frozenset[str] = frozenset(
    """
    UPI POS VPS ECOM ECOMM IPS CARD NEFT IMPS MMT RTGS ATW NWD CWD ATD ATM CHQ
    CHEQUE CLG CTS MICR NACH ECS ACH INB IB NETBANKING BIL BILLPAY ONL CASH CSH
    DR CR DEBIT CREDIT PAYMENT PAYMNT PMT PAY PAID TRANSFER TRF TXN TRANSACTION
    REF REFNO UTR RRN TID MID BATCH SEQ AUTH APPROVAL ORDER PURCHASE REVERSAL
    REFUND CHARGE CHARGES CHG FEE FEES GST IGST CGST SGST TAX TDS INT INTEREST
    OPENING CLOSING BALANCE B/F C/F BF CF TO FROM VIA FOR AND THE OF AT IN
    SELF OWN ACCOUNT AC ACCT A/C NO NUMBER BRANCH BANK LTD LIMITED PVT PRIVATE
    BY
    INDIA IN INR RS COLLECT REQUEST MANDATE SI STANDING INSTRUCTION AUTOPAY
    HDFC ICICI SBI AXIS KOTAK IDFC INDUSIND YES YESB PNB BOB CANARA UNION IDBI
    YBL OKAXIS OKICICI OKHDFCBANK OKSBI PAYTM IBL AXL UBIN SBIN PYTM APL
    SUCCESS SUCCESSFUL FAILED PENDING REVERSED
    P2A P2M P2P MB INET NET ONLINE OTHPG MIR CC WDL DEP DEPOSIT WITHDRAWAL
    SALARY RENT EMI ALERT ALERTS SMS CAPITALISED CAPITALIZED DEDUCTED
    THANK YOU RECEIVED ICIC HDFCBANK SBIN0 UTIB0 KKBK IDFB INDB SHCB
    """.split()
)

# Indian metros and common statement city suffixes on POS narrations. A card
# swipe prints the acquirer's city, which is not part of the merchant name.
_CITY_TOKENS: frozenset[str] = frozenset(
    """
    MUMBAI DELHI NEWDELHI BANGALORE BENGALURU HYDERABAD CHENNAI KOLKATA PUNE
    AHMEDABAD JAIPUR LUCKNOW SURAT KANPUR NAGPUR INDORE THANE BHOPAL VISAKHAPATNAM
    PATNA VADODARA GHAZIABAD LUDHIANA AGRA NASHIK FARIDABAD MEERUT RAJKOT
    GURGAON GURUGRAM NOIDA MYSORE MYSURU COIMBATORE KOCHI COCHIN CHANDIGARH
    """.split()
)

_NON_ALNUM = re.compile(r"[^A-Z0-9 ]+")
_EMBEDDED_DIGITS = re.compile(r"\d{4,}")
_WS = re.compile(r"\s+")


def _haystack(description: str) -> str:
    """Uppercase, separator-flattened form used for alias matching."""
    upper = textnorm.collapse(description).upper()
    return _WS.sub(" ", _NON_ALNUM.sub(" ", upper)).strip()


# --------------------------------------------------------------------------- #
# Alias index
# --------------------------------------------------------------------------- #

@dataclass(frozen=True, slots=True)
class MerchantEntry:
    slug: str
    name: str
    category_slug: str
    subcategory_slug: str | None
    subscription: bool
    mcc: str | None


@lru_cache(maxsize=1)
def _index() -> tuple[list[tuple[str, MerchantEntry]], dict[str, MerchantEntry]]:
    """Build the alias index once.

    The seed data is the single source of truth for the dictionary — the same
    list that populates the ``merchants`` table — so the parser and the database
    can never disagree about what "Swiggy" means.

    Aliases are sorted longest-first so ``SWIGGY INSTAMART`` is tested before
    ``SWIGGY`` and the more specific merchant wins.
    """
    from app.db.seed_data import MERCHANTS

    by_slug: dict[str, MerchantEntry] = {}
    aliases: list[tuple[str, MerchantEntry]] = []

    for seed in MERCHANTS:
        entry = MerchantEntry(
            slug=seed["slug"],
            name=seed["name"],
            category_slug=seed["category"],
            subcategory_slug=seed["subcategory"],
            subscription=seed["subscription"],
            mcc=seed["mcc"],
        )
        by_slug[entry.slug] = entry
        for alias in [*seed["aliases"], seed["name"]]:
            normalized = _WS.sub(" ", _NON_ALNUM.sub(" ", alias.upper())).strip()
            if normalized:
                aliases.append((normalized, entry))

    aliases.sort(key=lambda pair: (-len(pair[0]), pair[0]))
    return aliases, by_slug


def _alias_hit(haystack: str) -> tuple[MerchantEntry, str] | None:
    """Longest alias appearing as whole words in the narration."""
    padded = f" {haystack} "
    for alias, entry in _index()[0]:
        if f" {alias} " in padded:
            return entry, alias
    return None


# --------------------------------------------------------------------------- #
# Candidate extraction
# --------------------------------------------------------------------------- #

def _candidate(description: str) -> str:
    """Best guess at the merchant substring, with machine noise removed."""
    stripped = textnorm.strip_machine_noise(description)
    tokens = [
        token
        for token in _haystack(stripped).split()
        if token not in _STOP_TOKENS
        and token not in _CITY_TOKENS
        and not token.isdigit()
        # A token carrying a long digit run is a machine reference that survived
        # noise stripping (MIR2419000001, CITIN52410318), never a merchant.
        and not _EMBEDDED_DIGITS.search(token)
        and len(token) > 1
    ]
    return " ".join(tokens).strip()


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class MerchantMatch:
    """The outcome of normalization.

    ``is_known`` is the flag that matters downstream: True means this name came
    from the seeded dictionary and is a business, False means it is residue we
    tidied up and could be anything — including a person's name.
    """

    name: str | None
    slug: str | None
    confidence: float
    is_known: bool
    category_slug: str | None = None
    subcategory_slug: str | None = None
    subscription: bool = False
    mcc: str | None = None
    payment_method: PaymentMethod = PaymentMethod.UNKNOWN
    evidence: dict[str, Any] = field(default_factory=dict)


_NO_MERCHANT_METHODS = {PaymentMethod.ATM, PaymentMethod.CASH, PaymentMethod.CHEQUE}

_FUZZY_FLOOR = 88.0


def normalize_merchant(description: str) -> MerchantMatch:
    """Identify the merchant behind a narration.

    Confidence is deliberately banded rather than continuous:
    ``0.99`` dictionary, ``0.75–0.95`` fuzzy, ``0.50`` unidentified residue,
    ``0.90`` for the rails that legitimately have no merchant at all. A band
    means something a reviewer can act on; a continuous score invites false
    precision.
    """
    method = detect_payment_method(description)
    haystack = _haystack(description)

    hit = _alias_hit(haystack)
    if hit is not None:
        entry, alias = hit
        return MerchantMatch(
            name=entry.name,
            slug=entry.slug,
            confidence=0.99,
            is_known=True,
            category_slug=entry.category_slug,
            subcategory_slug=entry.subcategory_slug,
            subscription=entry.subscription,
            mcc=entry.mcc,
            payment_method=method,
            evidence={"match": "dictionary_alias", "alias": alias},
        )

    # An ATM withdrawal or a cheque has no merchant. Saying so is a correct
    # answer, not a failure, and it should not drag confidence down.
    if method in _NO_MERCHANT_METHODS:
        return MerchantMatch(
            name=None, slug=None, confidence=0.90, is_known=False,
            payment_method=method,
            evidence={"match": "no_merchant_for_rail", "rail": str(method)},
        )

    candidate = _candidate(description)

    # A UPI VPA local part is often a cleaner merchant name than the narration
    # body, so it gets its own pass through the dictionary.
    vpa_name = textnorm.upi_handle_name(description)
    if vpa_name:
        vpa_hit = _alias_hit(_haystack(vpa_name))
        if vpa_hit is not None:
            entry, alias = vpa_hit
            return MerchantMatch(
                name=entry.name, slug=entry.slug, confidence=0.97, is_known=True,
                category_slug=entry.category_slug,
                subcategory_slug=entry.subcategory_slug,
                subscription=entry.subscription, mcc=entry.mcc,
                payment_method=method,
                evidence={"match": "upi_handle", "alias": alias},
            )

    if candidate:
        fuzzy = _fuzzy_match(candidate)
        if fuzzy is not None:
            entry, score = fuzzy
            # Map 88–100 onto 0.75–0.95. A fuzzy match is never allowed to
            # reach dictionary confidence, however good the ratio looks.
            confidence = round(0.75 + (score - _FUZZY_FLOOR) / (100 - _FUZZY_FLOOR) * 0.20, 3)
            return MerchantMatch(
                name=entry.name, slug=entry.slug, confidence=confidence, is_known=True,
                category_slug=entry.category_slug,
                subcategory_slug=entry.subcategory_slug,
                subscription=entry.subscription, mcc=entry.mcc,
                payment_method=method,
                evidence={"match": "fuzzy", "score": round(score, 1)},
            )

    display = _titleise(candidate or vpa_name or "")
    return MerchantMatch(
        name=display or None,
        slug=None,
        confidence=0.50 if display else 0.30,
        is_known=False,
        payment_method=method,
        evidence={"match": "unmatched_residue"},
    )


def _fuzzy_match(candidate: str) -> tuple[MerchantEntry, float] | None:
    from rapidfuzz import fuzz, process

    aliases = _index()[0]
    result = process.extractOne(
        candidate,
        [alias for alias, _ in aliases],
        scorer=fuzz.token_set_ratio,
        score_cutoff=_FUZZY_FLOOR,
    )
    if result is None:
        return None
    _, score, position = result

    # token_set_ratio scores a one-word candidate against a one-word alias very
    # generously — "ZEPHYR" against "ZEPTO" clears 88 on the strength of shared
    # letters. Requiring a prefix or containment relationship for short
    # candidates keeps the guess honest.
    alias = aliases[position][0]
    if len(candidate) <= 6 or len(alias) <= 6:
        if not (candidate.startswith(alias) or alias.startswith(candidate)
                or alias in candidate or candidate in alias):
            return None

    return aliases[position][1], float(score)


def _titleise(value: str) -> str:
    """Present unmatched residue readably without claiming it is a known name."""
    if not value:
        return ""
    words = [word.capitalize() if word.isalpha() else word for word in value.split()]
    return " ".join(words)[:255]
