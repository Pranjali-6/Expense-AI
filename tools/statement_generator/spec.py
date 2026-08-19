"""Bank statement layout specifications.

Each spec describes how one bank prints a statement: its column set, its date
format, whether debits and credits get separate columns or share one with a
``Dr``/``Cr`` suffix, and — most importantly — how it formats a narration.

The narration styles are the part that earns its keep. Every Indian bank pushes
the same UPI transaction through a different template, and those templates are
exactly what a parser has to survive:

    HDFC   UPI-SWIGGY-SWIGGY@YBL-YESB0YBLUPI-412345678901-PAYMENT
    ICICI  UPI/412345678901/Payment/swiggy@ybl/YES BANK
    SBI    TO TRANSFER-UPI/DR/412345678901/SWIGGY/YESB/swiggy@ybl/Payment
    Axis   UPI/P2M/412345678901/SWIGGY/YES BANK

**Everything generated from these specs is fictional.** The merchant names are
real businesses, because a merchant dictionary that matched invented shops would
test nothing. Every person, account number, card number, address and transaction
is made up.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from app.models.enums import DocumentType

# --------------------------------------------------------------------------- #
# Amount column style
# --------------------------------------------------------------------------- #

SPLIT_COLUMNS = "split"      # separate Withdrawal / Deposit columns
SIGNED_SUFFIX = "suffix"     # one Amount column, Dr/Cr suffix
SIGNED_CREDIT_ONLY = "suffix_cr"  # one Amount column; only credits are marked
SIGNED_MINUS = "minus"       # one Amount column, leading minus for debits


@dataclass(frozen=True, slots=True)
class NarrationStyle:
    """How one bank templates each payment rail.

    Every callable takes the transaction's semantic fields and returns the
    string the bank would print. ``ref`` is a fictional reference number,
    ``card`` a fictional masked card number.
    """

    upi: Callable[[str, str, str], str]
    pos: Callable[[str, str, str], str]
    neft: Callable[[str, str], str]
    imps: Callable[[str, str], str]
    atm: Callable[[str, str], str]
    salary: Callable[[str, str], str]
    nach: Callable[[str, str], str]
    charge: Callable[[str], str]
    cheque: Callable[[str, str], str]
    card_payment: Callable[[str], str]
    #: Credit-direction variants. Banks that prefix narrations with a direction
    #: word (SBI's TO/BY) print something different when money arrives, and a
    #: fixture that ignored that would contradict its own statement.
    charge_credit: Callable[[str], str] | None = None
    refund: Callable[[str, str, str], str] | None = None


@dataclass(frozen=True, slots=True)
class BankSpec:
    code: str
    name: str
    legal_name: str
    ifsc_prefix: str
    #: Header lines printed above the transaction table.
    header_lines: tuple[str, ...]
    columns: tuple[str, ...]
    #: One role per column, parallel to ``columns``. Roles are:
    #: ``serial date value_date description reference debit credit balance
    #: amount branch points``. Declared explicitly rather than inferred from
    #: the header text, so the generator and the parsers share no synonym
    #: table — if a parser fails to recognise a column header, the accuracy
    #: harness must catch it rather than both sides agreeing on a private key.
    roles: tuple[str, ...]
    date_format: str
    amount_style: str
    narration: NarrationStyle
    document_type: DocumentType = DocumentType.BANK_STATEMENT
    footer_lines: tuple[str, ...] = ()
    #: Printed as "Statement Period" / "From ... To ..." etc.
    period_label: str = "Statement Period"
    opening_label: str = "Opening Balance"
    closing_label: str = "Closing Balance"
    #: Some banks print a transaction count; it gives the harness an
    #: independent recall check that does not depend on our own extraction.
    prints_transaction_count: bool = False
    font_size: float = 7.2
    extras: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Narration templates, per bank
# --------------------------------------------------------------------------- #

HDFC_NARRATION = NarrationStyle(
    upi=lambda payee, vpa, ref: f"UPI-{payee.upper()}-{vpa.upper()}-YESB0YBLUPI-{ref}-PAYMENT",
    pos=lambda payee, card, city: f"POS {card} {payee.upper()}",
    neft=lambda payee, ref: f"NEFT DR-HDFC0000123-{payee.upper()}-{ref}",
    imps=lambda payee, ref: f"IMPS-{ref}-{payee.upper()}-HDFC-XXXXXX4471",
    atm=lambda card, city: f"ATW-{card}-{city.upper()}-HDFC BANK",
    salary=lambda employer, ref: f"NEFT CR-CITI0000004-{employer.upper()}-SALARY-{ref}",
    nach=lambda payee, ref: f"ACH D- {payee.upper()}-{ref}",
    charge=lambda label: f"{label.upper()}-MIR2419000001",
    cheque=lambda payee, number: f"CHQ PAID-MICR CTS-{number}-{payee.upper()}",
    card_payment=lambda ref: f"CREDIT CARD PAYMENT-{ref}",
)

ICICI_NARRATION = NarrationStyle(
    upi=lambda payee, vpa, ref: f"UPI/{ref}/Payment/{vpa.lower()}/YES BANK",
    pos=lambda payee, card, city: f"VPS/{card}/{payee.upper()}/{city.upper()}",
    neft=lambda payee, ref: f"NEFT-{ref}-{payee.upper()}",
    imps=lambda payee, ref: f"IMPS/{ref}/{payee.upper()}/ICIC",
    atm=lambda card, city: f"ATM/CASH/{card}/{city.upper()}",
    salary=lambda employer, ref: f"NEFT-{ref}-{employer.upper()}-SALARY CREDIT",
    nach=lambda payee, ref: f"NACH/{payee.upper()}/{ref}",
    charge=lambda label: f"{label.upper()} CHARGES",
    cheque=lambda payee, number: f"CHEQUE PAID/{number}/{payee.upper()}",
    card_payment=lambda ref: f"BIL/ONL/{ref}/ICICI CREDIT CARD",
)

SBI_NARRATION = NarrationStyle(
    upi=lambda payee, vpa, ref: (
        f"TO TRANSFER-UPI/DR/{ref}/{payee.upper()}/YESB/{vpa.lower()}/Payment"
    ),
    pos=lambda payee, card, city: f"BY DEBIT CARD-OTHPG {payee.upper()} {city.upper()}",
    neft=lambda payee, ref: f"TO TRANSFER-NEFT*SBIN*{ref}*{payee.upper()}",
    imps=lambda payee, ref: f"TO TRANSFER-INB IMPS/{ref}/{payee.upper()}",
    atm=lambda card, city: f"BY CASH WDL-ATM {card} {city.upper()}",
    salary=lambda employer, ref: f"BY TRANSFER-NEFT*{ref}*{employer.upper()} SALARY",
    nach=lambda payee, ref: f"TO TRANSFER-ACH DR {payee.upper()} {ref}",
    charge=lambda label: f"TO TRANSFER-{label.upper()}",
    cheque=lambda payee, number: f"BY CHEQUE-{number} {payee.upper()}",
    card_payment=lambda ref: f"TO TRANSFER-INB CREDIT CARD PAYMENT {ref}",
    charge_credit=lambda label: f"BY TRANSFER-{label.upper()}",
    refund=lambda payee, vpa, ref: (
        f"BY TRANSFER-UPI/CR/{ref}/{payee.upper()}/YESB/{vpa.lower()}/Refund"
    ),
)

AXIS_NARRATION = NarrationStyle(
    upi=lambda payee, vpa, ref: f"UPI/P2M/{ref}/{payee.upper()}/YES BANK",
    pos=lambda payee, card, city: f"POS/{payee.upper()}/{city.upper()}/{card}",
    neft=lambda payee, ref: f"NEFT/{ref}/{payee.upper()}/UTIB",
    imps=lambda payee, ref: f"IMPS/P2A/{ref}/{payee.upper()}",
    atm=lambda card, city: f"ATM-CASH/{card}/{city.upper()}",
    salary=lambda employer, ref: f"NEFT/{ref}/{employer.upper()}/SALARY",
    nach=lambda payee, ref: f"ACH-DR-{payee.upper()}-{ref}",
    charge=lambda label: f"{label.upper()}:AXIS",
    cheque=lambda payee, number: f"CHQ PAID/{number}/{payee.upper()}",
    card_payment=lambda ref: f"IB BILLPAY DR-AXIS CC-{ref}",
)

KOTAK_NARRATION = NarrationStyle(
    upi=lambda payee, vpa, ref: f"UPI-{ref}-{vpa.upper()}-PAYMENT TO {payee.upper()}",
    pos=lambda payee, card, city: f"PCD/{card}/{payee.upper()}",
    neft=lambda payee, ref: f"NEFT OUT-{ref}-{payee.upper()}",
    imps=lambda payee, ref: f"MB IMPS-{ref}-{payee.upper()}",
    atm=lambda card, city: f"NWD-{card}-{city.upper()}",
    salary=lambda employer, ref: f"NEFT IN-{ref}-{employer.upper()}-SALARY",
    nach=lambda payee, ref: f"ACH/{payee.upper()}/{ref}",
    charge=lambda label: f"{label.upper()} DEDUCTED",
    cheque=lambda payee, number: f"CHQ DEP RET-{number}-{payee.upper()}",
    card_payment=lambda ref: f"KOTAK CC PAYMENT-{ref}",
)

GENERIC_NARRATION = NarrationStyle(
    upi=lambda payee, vpa, ref: f"UPI/{ref}/{payee.upper()}/{vpa.lower()}",
    pos=lambda payee, card, city: f"POS {card} {payee.upper()} {city.upper()}",
    neft=lambda payee, ref: f"NEFT {ref} {payee.upper()}",
    imps=lambda payee, ref: f"IMPS {ref} {payee.upper()}",
    atm=lambda card, city: f"ATM WDL {card} {city.upper()}",
    salary=lambda employer, ref: f"NEFT {ref} {employer.upper()} SALARY",
    nach=lambda payee, ref: f"NACH {payee.upper()} {ref}",
    charge=lambda label: label.upper(),
    cheque=lambda payee, number: f"CHEQUE {number} {payee.upper()}",
    card_payment=lambda ref: f"CREDIT CARD PAYMENT {ref}",
)

CARD_NARRATION = NarrationStyle(
    upi=lambda payee, vpa, ref: f"{payee.upper()}",
    pos=lambda payee, card, city: f"{payee.upper()}          {city.upper()}",
    neft=lambda payee, ref: f"{payee.upper()}",
    imps=lambda payee, ref: f"{payee.upper()}",
    atm=lambda card, city: f"CASH ADVANCE {city.upper()}",
    salary=lambda employer, ref: f"{employer.upper()}",
    nach=lambda payee, ref: f"{payee.upper()} AUTOPAY",
    charge=lambda label: label.upper(),
    cheque=lambda payee, number: f"{payee.upper()}",
    card_payment=lambda ref: "PAYMENT RECEIVED - THANK YOU",
)


# --------------------------------------------------------------------------- #
# The specs
# --------------------------------------------------------------------------- #

HDFC = BankSpec(
    code="HDFC",
    name="HDFC Bank",
    legal_name="HDFC BANK LIMITED",
    ifsc_prefix="HDFC",
    header_lines=(
        "HDFC BANK LIMITED",
        "Statement of Account",
    ),
    columns=("Date", "Narration", "Chq./Ref.No.", "Value Dt", "Withdrawal Amt.",
             "Deposit Amt.", "Closing Balance"),
    roles=("date", "description", "reference", "value_date", "debit", "credit", "balance"),
    date_format="%d/%m/%y",
    amount_style=SPLIT_COLUMNS,
    narration=HDFC_NARRATION,
    footer_lines=(
        "This is a computer generated statement and does not require signature.",
        "HDFC Bank Limited, Registered Office: HDFC Bank House, Mumbai 400013",
    ),
    period_label="Statement From",
    prints_transaction_count=True,
)

ICICI = BankSpec(
    code="ICICI",
    name="ICICI Bank",
    legal_name="ICICI BANK LIMITED",
    ifsc_prefix="ICIC",
    header_lines=(
        "ICICI Bank Limited",
        "Detailed Statement",
    ),
    columns=("S No.", "Value Date", "Transaction Date", "Cheque Number",
             "Transaction Remarks", "Withdrawal Amount (INR)",
             "Deposit Amount (INR)", "Balance (INR)"),
    roles=("serial", "value_date", "date", "reference", "description", "debit", "credit", "balance"),
    date_format="%d/%m/%Y",
    amount_style=SPLIT_COLUMNS,
    narration=ICICI_NARRATION,
    footer_lines=(
        "This is a system generated statement.",
        "ICICI Bank Limited, ICICI Bank Towers, Bandra Kurla Complex, Mumbai 400051",
    ),
    period_label="Statement Period",
    prints_transaction_count=False,
)

SBI = BankSpec(
    code="SBI",
    name="State Bank of India",
    legal_name="STATE BANK OF INDIA",
    ifsc_prefix="SBIN",
    header_lines=(
        "STATE BANK OF INDIA",
        "Account Statement",
    ),
    columns=("Txn Date", "Value Date", "Description", "Ref No./Cheque No.",
             "Debit", "Credit", "Balance"),
    roles=("date", "value_date", "description", "reference", "debit", "credit", "balance"),
    date_format="%d %b %Y",
    amount_style=SPLIT_COLUMNS,
    narration=SBI_NARRATION,
    footer_lines=(
        "This is a computer generated statement and does not require a signature.",
        "State Bank of India, Corporate Centre, Madame Cama Road, Mumbai 400021",
    ),
    period_label="Account Statement from",
    prints_transaction_count=True,
)

AXIS = BankSpec(
    code="AXIS",
    name="Axis Bank",
    legal_name="AXIS BANK LIMITED",
    ifsc_prefix="UTIB",
    header_lines=(
        "AXIS BANK LTD",
        "Statement of Account",
    ),
    columns=("Tran Date", "Chq No", "Particulars", "Debit", "Credit",
             "Balance", "Init.Br"),
    roles=("date", "reference", "description", "debit", "credit", "balance", "branch"),
    date_format="%d-%m-%Y",
    amount_style=SPLIT_COLUMNS,
    narration=AXIS_NARRATION,
    footer_lines=(
        "Statement generated electronically; no signature required.",
        "Axis Bank Ltd, Trishul, Opp. Samartheshwar Temple, Ahmedabad 380006",
    ),
    period_label="Statement of Account for the period",
    prints_transaction_count=False,
)

KOTAK = BankSpec(
    code="KOTAK",
    name="Kotak Mahindra Bank",
    legal_name="KOTAK MAHINDRA BANK LIMITED",
    ifsc_prefix="KKBK",
    header_lines=(
        "Kotak Mahindra Bank Ltd",
        "Statement of Account",
    ),
    # One amount column carrying its own direction — the layout that silently
    # inverts a statement if a parser ignores the Dr/Cr suffix.
    columns=("Date", "Narration", "Chq/Ref No", "Amount", "Balance"),
    roles=("date", "description", "reference", "amount", "balance"),
    date_format="%d-%b-%Y",
    amount_style=SIGNED_SUFFIX,
    narration=KOTAK_NARRATION,
    footer_lines=("Kotak Mahindra Bank Ltd, 27BKC, Bandra Kurla Complex, Mumbai 400051",),
    period_label="Period",
)

IDFC = BankSpec(
    code="IDFC",
    name="IDFC FIRST Bank",
    legal_name="IDFC FIRST BANK LIMITED",
    ifsc_prefix="IDFB",
    header_lines=("IDFC FIRST Bank Limited", "Account Statement"),
    columns=("Transaction Date", "Value Date", "Particulars", "Cheque no",
             "Debit", "Credit", "Balance"),
    roles=("date", "value_date", "description", "reference", "debit", "credit", "balance"),
    date_format="%d-%m-%Y",
    amount_style=SPLIT_COLUMNS,
    narration=GENERIC_NARRATION,
    footer_lines=("IDFC FIRST Bank Ltd, KRM Tower, Harrington Road, Chennai 600031",),
)

INDUSIND = BankSpec(
    code="INDUSIND",
    name="IndusInd Bank",
    legal_name="INDUSIND BANK LIMITED",
    ifsc_prefix="INDB",
    header_lines=("IndusInd Bank Limited", "Statement of Account"),
    columns=("Date", "Particulars", "Chq no", "Withdrawals", "Deposits", "Balance"),
    roles=("date", "description", "reference", "debit", "credit", "balance"),
    date_format="%d/%m/%Y",
    amount_style=SPLIT_COLUMNS,
    narration=GENERIC_NARRATION,
    footer_lines=("IndusInd Bank Ltd, 2401 Gen. Thimmayya Road, Pune 411001",),
)

YESBANK = BankSpec(
    code="YES",
    name="Yes Bank",
    legal_name="YES BANK LIMITED",
    ifsc_prefix="YESB",
    header_lines=("YES BANK Limited", "Account Statement"),
    columns=("Transaction Date", "Value Date", "Description", "Debit",
             "Credit", "Running Balance"),
    roles=("date", "value_date", "description", "debit", "credit", "balance"),
    date_format="%d/%m/%Y",
    amount_style=SPLIT_COLUMNS,
    narration=GENERIC_NARRATION,
    footer_lines=("YES BANK Ltd, YES BANK House, Santacruz East, Mumbai 400055",),
)

GENERIC_BANK = BankSpec(
    code="GENERIC",
    name="Sahyadri Cooperative Bank",
    legal_name="SAHYADRI COOPERATIVE BANK LTD",
    ifsc_prefix="SHCB",
    header_lines=("SAHYADRI COOPERATIVE BANK LTD", "Statement of Account"),
    columns=("Date", "Particulars", "Debit", "Credit", "Balance"),
    roles=("date", "description", "debit", "credit", "balance"),
    date_format="%d/%m/%Y",
    amount_style=SPLIT_COLUMNS,
    narration=GENERIC_NARRATION,
    footer_lines=("Registered Office: Shivaji Nagar, Pune 411005",),
)

HDFC_CARD = BankSpec(
    code="HDFC",
    name="HDFC Bank Credit Card",
    legal_name="HDFC BANK LIMITED",
    ifsc_prefix="HDFC",
    header_lines=("HDFC BANK LIMITED", "Credit Card Statement"),
    columns=("Date", "Transaction Description", "Amount (in Rs.)"),
    roles=("date", "description", "amount"),
    date_format="%d/%m/%Y",
    amount_style=SIGNED_SUFFIX,
    narration=CARD_NARRATION,
    document_type=DocumentType.CREDIT_CARD_STATEMENT,
    footer_lines=(
        "Please pay by the due date to avoid finance charges.",
        "HDFC Bank Cards Division, Chennai 600006",
    ),
    period_label="Statement Period",
)

ICICI_CARD = BankSpec(
    code="ICICI",
    name="ICICI Bank Credit Card",
    legal_name="ICICI BANK LIMITED",
    ifsc_prefix="ICIC",
    header_lines=("ICICI Bank Limited", "Credit Card Statement"),
    columns=("Date", "SerNo.", "Transaction Details", "Reward Points", "Amount (in Rs)"),
    roles=("date", "serial", "description", "points", "amount"),
    date_format="%d/%m/%Y",
    amount_style=SIGNED_CREDIT_ONLY,
    narration=CARD_NARRATION,
    document_type=DocumentType.CREDIT_CARD_STATEMENT,
    footer_lines=("ICICI Bank Cards, Mumbai 400051",),
    period_label="Statement Period",
)


ALL_SPECS: dict[str, BankSpec] = {
    "hdfc": HDFC,
    "icici": ICICI,
    "sbi": SBI,
    "axis": AXIS,
    "kotak": KOTAK,
    "idfc": IDFC,
    "indusind": INDUSIND,
    "yes": YESBANK,
    "generic": GENERIC_BANK,
    "hdfc_card": HDFC_CARD,
    "icici_card": ICICI_CARD,
}
