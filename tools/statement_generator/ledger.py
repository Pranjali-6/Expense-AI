"""Builds the fictional ledger a statement is rendered from.

The ledger is generated *first*, in exact Decimal arithmetic, with running
balances computed as it goes. The PDF is then rendered from it and the
``expected.json`` written from the same objects — so the ground truth is not a
transcription of the PDF, it is the thing the PDF was printed from. A
transcription could disagree with the document; this cannot.

Everything here is fictional. Merchant names are real Indian businesses because
a merchant dictionary tested against invented shops tests nothing, but every
person, account, card, reference number and transaction is made up.

Generation is seeded, so the same seed always produces the same statement. A
golden fixture that changed between runs would be worthless as a regression
test.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from app.models.enums import Direction, PaymentMethod

from tools.statement_generator.spec import BankSpec

# --------------------------------------------------------------------------- #
# Fictional cast
# --------------------------------------------------------------------------- #

ACCOUNT_HOLDERS: tuple[tuple[str, str], ...] = (
    ("Ananya Deshpande", "Flat 402, Sunrise Residency, Baner Road, Pune 411045"),
    ("Rohan Iyer", "12/A Lakeview Apartments, Indiranagar, Bengaluru 560038"),
    ("Meera Krishnan", "7 Gulmohar Enclave, Sector 14, Gurugram 122001"),
    ("Vikram Sethi", "301 Silver Oak Towers, Andheri West, Mumbai 400058"),
)

COUNTERPARTIES: tuple[str, ...] = (
    "Rahul Sharma", "Priya Nair", "Aditya Menon", "Sneha Kulkarni",
    "Karthik Reddy", "Divya Bhatt",
)

#: (printed on the statement, expected after normalization). A corporate
#: suffix is not part of a merchant's normalized name — "Everstack Technologies
#: Pvt Ltd" and "Everstack Technologies" must not become two employers.
EMPLOYERS: tuple[tuple[str, str], ...] = (
    ("Everstack Technologies Pvt Ltd", "Everstack Technologies"),
    ("Nimbus Analytics India", "Nimbus Analytics"),
    ("Kaveri Systems Ltd", "Kaveri Systems"),
)

CITIES: tuple[str, ...] = ("BANGALORE", "MUMBAI", "PUNE", "GURGAON", "CHENNAI")


@dataclass(slots=True)
class LedgerEntry:
    """One generated transaction, with its ground truth attached."""

    txn_date: date
    value_date: date
    description: str
    amount: Decimal
    direction: Direction
    reference: str
    # Ground truth, decided at generation time — not derived from the narration.
    merchant: str | None
    merchant_slug: str | None
    category_slug: str | None
    subcategory_slug: str | None
    payment_method: PaymentMethod
    balance_after: Decimal = Decimal("0.00")


@dataclass(slots=True)
class GeneratedStatement:
    spec: BankSpec
    holder_name: str
    holder_address: str
    account_number: str
    account_last4: str
    card_masked: str
    ifsc: str
    branch: str
    period_start: date
    period_end: date
    opening_balance: Decimal
    closing_balance: Decimal
    entries: list[LedgerEntry] = field(default_factory=list)
    # Card statements only.
    total_due: Decimal | None = None
    minimum_due: Decimal | None = None
    payment_due_date: date | None = None
    credit_limit: Decimal | None = None
    notes: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Spend profile
#
# `(merchant, slug, category, subcategory, rail, low, high, per_month)` —
# amounts in whole rupees, `per_month` the expected frequency. The alias column
# is what actually gets printed, and it is deliberately often *not* the
# merchant's display name: a statement says BUNDL TECHNOLOGIES, not Swiggy.
# --------------------------------------------------------------------------- #

@dataclass(frozen=True, slots=True)
class SpendTemplate:
    printed: str            # what the narration says
    merchant: str           # ground-truth normalized name
    slug: str
    category: str
    subcategory: str | None
    rail: str
    low: int
    high: int
    per_month: float
    vpa: str = ""


RECURRING: tuple[SpendTemplate, ...] = (
    SpendTemplate("NETFLIX", "Netflix", "netflix", "subscriptions", "ott", "nach", 199, 649, 1.0),
    SpendTemplate("SPOTIFY", "Spotify", "spotify", "subscriptions", "music", "nach", 119, 179, 1.0),
    SpendTemplate("AMAZON PRIME", "Amazon Prime", "amazon_prime", "subscriptions", "ott", "nach", 299, 1499, 0.35),
    SpendTemplate("ACT FIBERNET", "ACT Fibernet", "act_fibernet", "utilities", "internet", "nach", 799, 1499, 1.0),
    SpendTemplate("BESCOM", "BESCOM", "bescom", "utilities", "electricity", "nach", 850, 3400, 1.0),
)

FREQUENT: tuple[SpendTemplate, ...] = (
    SpendTemplate("BUNDL TECHNOLOGIES", "Swiggy", "swiggy", "food", "food_delivery", "upi", 180, 950, 6.0, "swiggy@ybl"),
    SpendTemplate("ZOMATO ONLINE", "Zomato", "zomato", "food", "food_delivery", "upi", 160, 880, 4.0, "zomato@paytm"),
    SpendTemplate("HANDS ON TRADES", "Blinkit", "blinkit", "grocery", "quick_commerce", "upi", 220, 1650, 5.0, "blinkit@ybl"),
    SpendTemplate("SWIGGY INSTAMART", "Swiggy Instamart", "instamart", "grocery", "quick_commerce", "upi", 250, 1400, 3.0, "instamart@ybl"),
    SpendTemplate("KIRANAKART", "Zepto", "zepto", "grocery", "quick_commerce", "upi", 180, 1200, 2.0, "zepto@ybl"),
    SpendTemplate("STARBUCKS", "Starbucks", "starbucks", "food", "cafes", "pos", 260, 780, 1.5),
    SpendTemplate("DOMINOS", "Domino's Pizza", "dominos", "food", "restaurants", "pos", 350, 1250, 1.5),
    SpendTemplate("DMART", "DMart", "dmart", "grocery", "supermarket", "pos", 900, 4800, 1.2),
    SpendTemplate("AMAZON", "Amazon", "amazon", "shopping", "online_retail", "upi", 340, 8500, 3.0, "amazon@apl"),
    SpendTemplate("FLIPKART", "Flipkart", "flipkart", "shopping", "online_retail", "upi", 420, 6400, 1.5, "flipkart@axl"),
    SpendTemplate("UBER INDIA", "Uber", "uber", "travel", "cabs", "upi", 90, 640, 4.0, "uber@axisbank"),
    SpendTemplate("OLA CABS", "Ola", "ola", "travel", "cabs", "upi", 85, 580, 2.0, "olacabs@ybl"),
    SpendTemplate("INDIAN OIL", "Indian Oil", "indian_oil", "fuel", "petrol", "pos", 800, 3600, 1.5),
    SpendTemplate("BOOKMYSHOW", "BookMyShow", "bookmyshow", "entertainment", "movies", "upi", 320, 1450, 0.8, "bookmyshow@ybl"),
    SpendTemplate("APOLLO PHARMACY", "Apollo Pharmacy", "apollo_pharmacy", "healthcare", "pharmacy", "pos", 210, 2400, 0.8),
    SpendTemplate("IRCTC", "IRCTC", "irctc", "travel", "trains", "upi", 450, 3200, 0.5, "irctc@sbi"),
)


def _amount(rng: random.Random, low: int, high: int) -> Decimal:
    """A realistic rupee amount, mostly with paise, sometimes round."""
    rupees = rng.randint(low, high)
    if rng.random() < 0.55:
        return Decimal(f"{rupees}.{rng.randint(0, 99):02d}")
    return Decimal(f"{rupees}.00")


def _ref(rng: random.Random, width: int = 12) -> str:
    return "".join(str(rng.randint(0, 9)) for _ in range(width))


def _billing_day(slug: str, days_in_month: int) -> int:
    """A stable billing day for a subscription, from its merchant name.

    Derived from the slug rather than the RNG so it is identical across months
    and across statements, which is what a real billing cycle looks like.
    Clamped to the month so a 31st-of-the-month subscription still bills in
    February — the same one-or-two-day drift real billing shows, and enough to
    exercise the detector's tolerance for it.
    """
    import zlib

    day = (zlib.crc32(slug.encode("utf-8")) % 27) + 2
    return min(day, days_in_month)


def _narrate(spec: BankSpec, rail: str, template: SpendTemplate,
             rng: random.Random) -> tuple[str, PaymentMethod]:
    style = spec.narration
    ref = _ref(rng)
    city = rng.choice(CITIES)
    card = f"4{_ref(rng, 3)}XXXXXXXX{_ref(rng, 4)}"

    if rail == "upi":
        return style.upi(template.printed, template.vpa or f"{template.slug}@ybl", ref), PaymentMethod.UPI
    if rail == "pos":
        return style.pos(template.printed, card, city), PaymentMethod.CARD
    if rail == "nach":
        return style.nach(template.printed, ref), PaymentMethod.NACH
    return style.neft(template.printed, ref), PaymentMethod.NEFT


def build_statement(
    spec: BankSpec,
    *,
    seed: int,
    period_start: date,
    period_end: date,
    opening_balance: Decimal,
    holder_index: int = 0,
    lakh_scale: bool = False,
    salary: Decimal | None = None,
    employer: tuple[str, str] | None = None,
    account_number: str | None = None,
) -> GeneratedStatement:
    """Generate one month of fictional bank activity.

    ``salary``, ``employer`` and ``account_number`` pin what is otherwise
    redrawn from the seed. The golden fixtures want variety — each is a
    different bank and a different person, and fixing these would make them all
    look alike. A *series* of months for one household wants the opposite: a
    person does not change employer or bank account every month. Left unpinned,
    six consecutive statements resolve to six different accounts, and what
    should be one account's history becomes six unrelated months that happen to
    be adjacent.
    """
    rng = random.Random(seed)
    holder_name, holder_address = ACCOUNT_HOLDERS[holder_index % len(ACCOUNT_HOLDERS)]

    # Drawn from the seed unless pinned, and drawn either way so the rest of the
    # month's random stream is identical whether or not a caller pinned it.
    drawn_account = f"{rng.randint(10, 99)}{_ref(rng, 12)}"
    account_number = account_number or drawn_account
    card_masked = f"4{_ref(rng, 3)}XXXXXXXX{_ref(rng, 4)}"
    statement = GeneratedStatement(
        spec=spec,
        holder_name=holder_name,
        holder_address=holder_address,
        account_number=account_number,
        account_last4=account_number[-4:],
        card_masked=card_masked,
        ifsc=f"{spec.ifsc_prefix}0{_ref(rng, 3)}{rng.randint(100, 999)}",
        branch=rng.choice(("Koramangala", "Bandra West", "Baner", "Cyber City", "T Nagar")),
        period_start=period_start,
        period_end=period_end,
        opening_balance=opening_balance,
        closing_balance=opening_balance,
    )

    days = (period_end - period_start).days + 1
    entries: list[LedgerEntry] = []

    def add(
        day_offset: int, description: str, amount: Decimal, direction: Direction,
        *, merchant: str | None, slug: str | None, category: str | None,
        subcategory: str | None, method: PaymentMethod, reference: str,
        value_offset: int = 0,
    ) -> None:
        txn_day = period_start + timedelta(days=min(max(day_offset, 0), days - 1))
        entries.append(
            LedgerEntry(
                txn_date=txn_day,
                value_date=txn_day + timedelta(days=value_offset),
                description=description,
                amount=amount,
                direction=direction,
                reference=reference,
                merchant=merchant,
                merchant_slug=slug,
                category_slug=category,
                subcategory_slug=subcategory,
                payment_method=method,
            )
        )

    style = spec.narration

    # --- salary, on the first working day -----------------------------------
    employer_printed, employer_normalized = employer or rng.choice(EMPLOYERS)
    # Drawn from the seed unless pinned. Consumed either way so the rest of the
    # month's random stream is unaffected — a pinned salary must not silently
    # change every other transaction in the statement.
    drawn_salary = Decimal(f"{rng.randint(85000, 240000)}.00")
    salary = salary if salary is not None else drawn_salary
    ref = _ref(rng)
    add(0, style.salary(employer_printed, ref), salary, Direction.CREDIT,
        merchant=employer_normalized, slug=None, category="salary",
        subcategory="monthly_salary", method=PaymentMethod.NEFT, reference=ref)

    # --- rent, a lakh-grouped amount so the digit grouping is exercised -----
    rent = Decimal(f"{rng.randint(18000, 32000)}.00") if not lakh_scale else Decimal(
        f"{rng.randint(125000, 340000)}.00"
    )
    ref = _ref(rng)
    landlord = rng.choice(COUNTERPARTIES)
    add(4, style.imps(f"{landlord} RENT", ref), rent, Direction.DEBIT,
        merchant=landlord, slug=None, category="rent", subcategory="house_rent",
        method=PaymentMethod.IMPS, reference=ref)

    # --- EMI -----------------------------------------------------------------
    emi = Decimal(f"{rng.randint(8500, 34000)}.00")
    ref = _ref(rng)
    add(6, style.nach("HDB FINANCIAL SERVICES", ref), emi, Direction.DEBIT,
        merchant="HDB Financial Services", slug=None, category="emi",
        subcategory="personal_loan", method=PaymentMethod.NACH, reference=ref)

    # --- recurring subscriptions and bills -----------------------------------
    # Billed on a *stable* day of the month, derived from the merchant rather
    # than drawn at random. A random billing day is not merely less realistic —
    # it makes a subscription undetectable, because irregular gaps are exactly
    # what distinguishes recurring spending from ordinary spending. Generating
    # fixtures that no correct detector could ever recognise would test nothing.
    for template in RECURRING:
        if rng.random() > template.per_month:
            continue
        billing_day = _billing_day(template.slug, days)
        description, method = _narrate(spec, template.rail, template, rng)
        add(billing_day - 1, description,
            _amount(rng, template.low, template.high), Direction.DEBIT,
            merchant=template.merchant, slug=template.slug,
            category=template.category, subcategory=template.subcategory,
            method=method, reference=_ref(rng))

    # --- everyday spending ---------------------------------------------------
    for template in FREQUENT:
        count = int(template.per_month)
        if rng.random() < (template.per_month - count):
            count += 1
        for _ in range(count):
            description, method = _narrate(spec, template.rail, template, rng)
            add(rng.randint(0, days - 1), description,
                _amount(rng, template.low, template.high), Direction.DEBIT,
                merchant=template.merchant, slug=template.slug,
                category=template.category, subcategory=template.subcategory,
                method=method, reference=_ref(rng))

    # --- ATM withdrawals: a merchant-less rail -------------------------------
    for _ in range(rng.randint(1, 3)):
        ref = _ref(rng)
        add(rng.randint(0, days - 1),
            style.atm(card_masked, rng.choice(CITIES)),
            Decimal(f"{rng.choice((2000, 3000, 5000, 10000))}.00"), Direction.DEBIT,
            merchant=None, slug=None, category="cash_withdrawal", subcategory="atm",
            method=PaymentMethod.ATM, reference=ref)

    # --- a person-to-person transfer: ground truth is a person's name --------
    for _ in range(rng.randint(1, 3)):
        ref = _ref(rng)
        person = rng.choice(COUNTERPARTIES)
        add(rng.randint(0, days - 1), style.imps(person, ref),
            _amount(rng, 500, 12000), Direction.DEBIT,
            merchant=person, slug=None, category=None, subcategory=None,
            method=PaymentMethod.IMPS, reference=ref)

    # --- credit-card settlement ----------------------------------------------
    ref = _ref(rng)
    add(rng.randint(15, min(24, days - 1)), style.card_payment(ref),
        _amount(rng, 8000, 90000), Direction.DEBIT,
        merchant=None, slug=None, category="credit_card_payment", subcategory=None,
        method=PaymentMethod.NETBANKING, reference=ref)

    # --- bank charges --------------------------------------------------------
    if rng.random() < 0.6:
        add(days - 2, style.charge("SMS ALERT CHARGES"), Decimal("17.70"),
            Direction.DEBIT, merchant=None, slug=None, category="bank_charges",
            subcategory="service_charges", method=PaymentMethod.INTERNAL,
            reference=_ref(rng, 8))

    # --- a refund: a credit that is not income -------------------------------
    if rng.random() < 0.5:
        ref = _ref(rng)
        template = rng.choice((FREQUENT[8], FREQUENT[9]))  # Amazon / Flipkart
        if style.refund is not None:
            description = style.refund(
                template.printed, template.vpa or f"{template.slug}@ybl", ref
            )
            method = PaymentMethod.UPI
        else:
            description, method = _narrate(spec, "upi", template, rng)
            description = f"{description}-REFUND"
        add(rng.randint(5, days - 1), description,
            _amount(rng, 300, 4200), Direction.CREDIT,
            merchant=template.merchant, slug=template.slug,
            category="refund", subcategory="purchase_refund",
            method=method, reference=ref)

    # --- interest credit -----------------------------------------------------
    interest_narration = (style.charge_credit or style.charge)("CREDIT INTEREST CAPITALISED")
    add(days - 1, interest_narration,
        _amount(rng, 120, 1800), Direction.CREDIT,
        merchant=None, slug=None, category="other", subcategory=None,
        method=PaymentMethod.INTERNAL, reference=_ref(rng, 8))

    # Chronological, with a stable tiebreak so the same seed always renders the
    # same order — a fixture whose row order drifts is not a golden fixture.
    entries.sort(key=lambda entry: (entry.txn_date, entry.reference))

    balance = opening_balance
    for entry in entries:
        balance = balance - entry.amount if entry.direction == Direction.DEBIT else balance + entry.amount
        entry.balance_after = balance

    statement.entries = entries
    statement.closing_balance = balance
    return statement


def build_card_statement(
    spec: BankSpec,
    *,
    seed: int,
    period_start: date,
    period_end: date,
    holder_index: int = 0,
    card_number: str | None = None,
) -> GeneratedStatement:
    """Generate a credit-card statement.

    Card statements reconcile differently: there is no running balance per row,
    and the arithmetic that must hold is
    ``previous balance + purchases − payments − credits = total amount due``.
    """
    rng = random.Random(seed)
    holder_name, holder_address = ACCOUNT_HOLDERS[holder_index % len(ACCOUNT_HOLDERS)]
    # Same reasoning as the savings account: a card that changes
    # number every month is twelve cards, not one card's history.
    drawn_card = f"4{_ref(rng, 3)}XXXXXXXX{_ref(rng, 4)}"
    card_masked = card_number or drawn_card

    previous_balance = _amount(rng, 4000, 60000)
    statement = GeneratedStatement(
        spec=spec,
        holder_name=holder_name,
        holder_address=holder_address,
        account_number=card_masked,
        account_last4=card_masked[-4:],
        card_masked=card_masked,
        ifsc="",
        branch="",
        period_start=period_start,
        period_end=period_end,
        opening_balance=previous_balance,
        closing_balance=previous_balance,
        credit_limit=Decimal(f"{rng.choice((150000, 250000, 400000, 600000))}.00"),
        payment_due_date=period_end + timedelta(days=18),
    )

    days = (period_end - period_start).days + 1
    entries: list[LedgerEntry] = []
    style = spec.narration

    def add(offset: int, description: str, amount: Decimal, direction: Direction,
            *, merchant: str | None, slug: str | None, category: str | None,
            subcategory: str | None, method: PaymentMethod) -> None:
        day = period_start + timedelta(days=min(max(offset, 0), days - 1))
        entries.append(
            LedgerEntry(
                txn_date=day, value_date=day, description=description,
                amount=amount, direction=direction, reference="",
                merchant=merchant, merchant_slug=slug, category_slug=category,
                subcategory_slug=subcategory, payment_method=method,
            )
        )

    # The settlement of last month's bill, as a credit.
    payment = previous_balance.quantize(Decimal("1")) if rng.random() < 0.7 else _amount(rng, 2000, 30000)
    add(rng.randint(2, 12), style.card_payment(""), payment, Direction.CREDIT,
        merchant=None, slug=None, category="credit_card_payment", subcategory=None,
        method=PaymentMethod.NETBANKING)

    for template in FREQUENT:
        count = int(template.per_month * 0.8)
        if rng.random() < 0.4:
            count += 1
        for _ in range(count):
            add(rng.randint(0, days - 1),
                style.pos(template.printed, card_masked, rng.choice(CITIES)),
                _amount(rng, template.low, template.high), Direction.DEBIT,
                merchant=template.merchant, slug=template.slug,
                category=template.category, subcategory=template.subcategory,
                method=PaymentMethod.CARD)

    # A refund. On a card statement this prints with a `Cr` suffix, which is the
    # single most common way a card parser silently doubles someone's spending.
    if rng.random() < 0.8:
        template = rng.choice(FREQUENT)
        add(rng.randint(4, days - 1),
            f"{template.printed.upper()} REFUND",
            _amount(rng, 400, 5200), Direction.CREDIT,
            merchant=template.merchant, slug=template.slug,
            category="refund", subcategory="purchase_refund",
            method=PaymentMethod.CARD)

    add(days - 1, "GST ON FINANCE CHARGES", _amount(rng, 20, 460), Direction.DEBIT,
        merchant=None, slug=None, category="bank_charges", subcategory="service_charges",
        method=PaymentMethod.INTERNAL)

    entries.sort(key=lambda entry: (entry.txn_date, entry.description))

    purchases = sum(
        (entry.amount for entry in entries if entry.direction == Direction.DEBIT),
        Decimal("0.00"),
    )
    credits = sum(
        (entry.amount for entry in entries if entry.direction == Direction.CREDIT),
        Decimal("0.00"),
    )
    total_due = (previous_balance + purchases - credits).quantize(Decimal("0.01"))

    statement.entries = entries
    statement.closing_balance = total_due
    statement.total_due = total_due
    statement.minimum_due = max(
        (total_due * Decimal("0.05")).quantize(Decimal("0.01")), Decimal("100.00")
    ) if total_due > 0 else Decimal("0.00")
    return statement
