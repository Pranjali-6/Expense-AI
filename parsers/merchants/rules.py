"""Deterministic categorisation rules.

The tier of the cascade that reads *structure* rather than merchant identity.
Some transactions are categorisable with certainty from the narration alone, and
for those a rule is not merely cheaper than a model — it is strictly better,
because it is auditable, reproducible, and cannot hallucinate.

    ATW-4123XXXXXXXX8842-BANGALORE-HDFC     → Cash Withdrawal, no merchant
    TO TRANSFER-INB CREDIT CARD PAYMENT …   → Credit Card Payment, not an expense
    AMAZON …-REFUND                          → Refund, not Shopping
    SMS ALERT CHARGES-MIR2419000001          → Bank Charges, no merchant

Three things these rules decide that the merchant dictionary cannot:

**Structure beats identity.** A refund of an Amazon purchase is a *refund*, not
shopping. The merchant is still Amazon, but the category is not.

**Some rows have no merchant.** An ATM withdrawal, a bank charge and an interest
credit have a rail and an amount and nothing that is a business. Emitting the
leftover words as a "merchant" — ``Sms Alert``, ``Capitalised`` — creates junk
that pollutes spend-by-merchant analytics and, worse, is exactly the kind of
unverified string the privacy gateway must never send anywhere.

**Not everything is spending.** ``is_expense`` is set here, and getting it wrong
is how a personal-finance tool reports that someone spent twice their income: a
credit-card payment settles purchases that were already counted, and moving
money between your own accounts is not spending at all.

This module is why the product still categorises an Indian bank statement
correctly with ``AI_ENABLED=false``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.models.enums import MovementType


@dataclass(frozen=True, slots=True)
class DeterministicRule:
    code: str
    pattern: re.Pattern[str]
    category_slug: str | None
    subcategory_slug: str | None
    movement: MovementType
    is_expense: bool
    #: True when the row has no merchant at all and any residue must be dropped.
    suppress_merchant: bool = False
    #: True when the rule decides the category outright, overriding whatever the
    #: merchant dictionary suggests. A refund is the motivating case.
    overrides_merchant_category: bool = True


def _rule(
    code: str, pattern: str, category: str | None, subcategory: str | None,
    movement: MovementType, *, is_expense: bool, suppress_merchant: bool = False,
    overrides: bool = True,
) -> DeterministicRule:
    return DeterministicRule(
        code=code,
        pattern=re.compile(pattern, re.IGNORECASE),
        category_slug=category,
        subcategory_slug=subcategory,
        movement=movement,
        is_expense=is_expense,
        suppress_merchant=suppress_merchant,
        overrides_merchant_category=overrides,
    )


# Ordered: the first match wins. Order is load-bearing — a credit-card payment
# narration also contains the word "payment", and a refund of a card purchase
# also mentions the card.
RULES: tuple[DeterministicRule, ...] = (
    # --- settlements and internal movement, none of which is spending -------
    _rule(
        "credit_card_payment",
        r"\bcredit\s*card\b|\bcc\s*(?:payment|bill|pmt)\b"
        r"|\bcard\s*payment\b|billpay.*\bcc\b|\bpayment\s+received\b"
        r"|\bcc\s*-\s*\d|\bcc\b(?=\s*-)",
        "credit_card_payment", None, MovementType.CREDIT_CARD_PAYMENT,
        is_expense=False, suppress_merchant=True,
    ),
    _rule(
        "cash_withdrawal",
        r"\batw\b|\bnwd\b|\bcwd\b|\batd\b|cash\s*wdl|atm\s*(?:cash|wdl|withdrawal)"
        r"|cash\s*withdrawal|\batm\b\s*[-/]|by\s+cash\s+wdl|cash\s+advance",
        "cash_withdrawal", "atm", MovementType.CASH_WITHDRAWAL,
        is_expense=False, suppress_merchant=True,
    ),
    _rule(
        "self_transfer",
        r"\bself\b|own\s+account|transfer\s+to\s+own|\bsweep\b|\blien\b",
        "transfers", None, MovementType.TRANSFER, is_expense=False,
    ),

    # --- income and returns --------------------------------------------------
    _rule(
        "salary",
        r"\bsalary\b|\bsal\s*cr\b|\bpayroll\b|\bwages\b|salary\s*credit",
        "salary", "monthly_salary", MovementType.SALARY, is_expense=False,
    ),
    _rule(
        "refund",
        r"\brefund\b|\breversal\b|\breversed\b|\bchargeback\b|\bcashback\b",
        "refund", "purchase_refund", MovementType.REFUND, is_expense=False,
    ),
    _rule(
        "interest_credit",
        r"credit\s+interest|interest\s+capitalis|int\.?\s*cr\b|savings?\s+interest"
        r"|\bint\s+pd\b",
        "other", None, MovementType.INCOME, is_expense=False, suppress_merchant=True,
    ),

    # --- costs the bank imposes ---------------------------------------------
    _rule(
        "bank_charges",
        r"sms\s*alert|\bamb\s*(?:chg|charge)|service\s*charge|min(?:imum)?\s*bal"
        r"|\bcharges?\b|\bchg\b|\bpenalty\b|annual\s*fee|\bgst\s+on\b|processing\s*fee"
        r"|late\s*(?:payment\s*)?fee|finance\s*charge|cheque\s*return|\bdebit\s*card\s*fee",
        "bank_charges", "service_charges", MovementType.BANK_CHARGE,
        is_expense=True, suppress_merchant=True,
    ),
    _rule(
        "taxes",
        r"\btds\b|income\s*tax|advance\s*tax|\bgst\s*payment\b|self\s*assessment",
        "taxes", None, MovementType.EXPENSE, is_expense=True, suppress_merchant=True,
    ),

    # --- borrowing and saving ------------------------------------------------
    _rule(
        "emi",
        r"\bemi\b|loan\s*(?:repay|instal|emi)|equated\s*monthly|\bhdb\s*financial"
        r"|bajaj\s*fin|\bhome\s*loan\b|\bauto\s*loan\b|\bpersonal\s*loan\b",
        "emi", "personal_loan", MovementType.EMI, is_expense=True,
    ),
    _rule(
        "investment",
        r"\bsip\b|mutual\s*fund|\bmf\s*purchase|\bnps\b|\bppf\b|recurring\s*deposit"
        r"|\bfd\s*(?:booking|created)|zerodha|groww|\bcoin\b\s*mf",
        "investment", None, MovementType.INVESTMENT, is_expense=False,
    ),
    _rule(
        "insurance",
        r"\blic\b|life\s*insurance|health\s*insurance|\bpolicy\s*premium\b"
        r"|insurance\s*premium|\bhdfc\s*life\b|\bmax\s*life\b",
        "insurance", None, MovementType.EXPENSE, is_expense=True,
    ),

    # --- housing -------------------------------------------------------------
    # Rent only when the narration says so. An unannotated IMPS transfer to a
    # person is genuinely indistinguishable from any other transfer, and
    # guessing "rent" because the amount looks rent-shaped would be inventing
    # information the document does not contain.
    _rule(
        "rent",
        r"\brent\b|house\s*rent|\brental\b|maintenance\s*charge|society\s*maint",
        "rent", "house_rent", MovementType.EXPENSE, is_expense=True,
    ),
)


@dataclass(frozen=True, slots=True)
class RuleMatch:
    rule: DeterministicRule
    matched_text: str


def match_rule(description: str) -> RuleMatch | None:
    """First matching rule, or None."""
    for rule in RULES:
        found = rule.pattern.search(description)
        if found:
            return RuleMatch(rule=rule, matched_text=found.group(0))
    return None
