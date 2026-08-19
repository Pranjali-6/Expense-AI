"""Does the statement's arithmetic close?

This is the check that decides whether anything downstream may be believed. A
statement earns ``trust_status = trusted`` on an **exact ₹0.00** delta and on
nothing else. There is no tolerance band, because there is no amount of money
that is acceptably missing from someone's ledger, and a band would silently
absorb exactly the parser bug it exists to catch.

Three independent checks, because they fail differently:

* **Totals.** ``opening − debits + credits = closing``. Catches a lost or
  invented row anywhere in the statement, but says nothing about where.
* **Running balance continuity.** Row by row, the printed balance must move by
  exactly the transaction's amount. This is what turns "₹4,955.94 unaccounted"
  into "row 23 on page 2", which is the difference between a report a person can
  act on and one they can only be alarmed by.
* **Page continuity.** Pages 1..N all present. A statement missing page 3 can
  still balance if the missing page's debits and credits happen to cancel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Sequence

from app.models.enums import Direction, DocumentType, TrustStatus

from parsers.canonical import CanonicalTransaction, StatementMetadata

ZERO = Decimal("0.00")


@dataclass(slots=True)
class ReconciliationReport:
    #: True only on an exact zero delta.
    reconciles: bool = False
    #: None when the statement did not print enough to check. Distinct from
    #: zero, which claims a balance that was actually verified.
    delta: Decimal | None = None
    total_debits: Decimal = ZERO
    total_credits: Decimal = ZERO

    balance_continuous: bool = False
    balance_checked: bool = False
    first_divergent_row: int | None = None
    first_divergent_page: int | None = None

    pages_continuous: bool = True
    missing_pages: list[int] = field(default_factory=list)

    #: Rows dated outside the printed statement period. A signal, never a filter:
    #: an out-of-period row is flagged, not dropped.
    out_of_period: int = 0

    @property
    def trust_status(self) -> TrustStatus:
        """Trusted requires proof, not merely the absence of a contradiction."""
        if self.delta is None:
            # Nothing to check against. Not trusted, and not accused either —
            # the health report says which of the two this is.
            return TrustStatus.PENDING
        return TrustStatus.TRUSTED if self.reconciles else TrustStatus.UNTRUSTED

    @property
    def unverifiable(self) -> bool:
        return self.delta is None


def _totals(transactions: Sequence[CanonicalTransaction]) -> tuple[Decimal, Decimal]:
    debits = sum(
        (t.amount for t in transactions if t.direction == Direction.DEBIT), ZERO
    )
    credits = sum(
        (t.amount for t in transactions if t.direction == Direction.CREDIT), ZERO
    )
    return Decimal(debits).quantize(Decimal("0.01")), Decimal(credits).quantize(Decimal("0.01"))


def reconcile(
    metadata: StatementMetadata,
    transactions: Sequence[CanonicalTransaction],
    *,
    page_count: int | None = None,
) -> ReconciliationReport:
    report = ReconciliationReport()
    report.total_debits, report.total_credits = _totals(transactions)

    is_card = metadata.document_type == DocumentType.CREDIT_CARD_STATEMENT
    opening = metadata.opening_balance
    closing = metadata.total_amount_due if is_card else metadata.closing_balance

    if opening is not None and closing is not None:
        if is_card:
            # A card bill grows with purchases and shrinks with payments, which
            # is the opposite sign convention to a deposit account. Using the
            # bank identity here reports every card statement as broken by
            # exactly twice its month's spending.
            computed = opening + report.total_debits - report.total_credits
        else:
            computed = opening - report.total_debits + report.total_credits

        report.delta = (computed - closing).quantize(Decimal("0.01"))
        report.reconciles = report.delta == ZERO

    _check_balance_continuity(report, opening, transactions)
    _check_pages(report, page_count, transactions)

    if metadata.period_start and metadata.period_end:
        from parsers.normalizers.dates import within

        report.out_of_period = sum(
            1
            for t in transactions
            if not within(t.txn_date, metadata.period_start, metadata.period_end, slack_days=3)
        )

    return report


def _check_balance_continuity(
    report: ReconciliationReport,
    opening: Decimal | None,
    transactions: Sequence[CanonicalTransaction],
) -> None:
    """Walk the running balance and find where it first stops following.

    Only meaningful when the statement prints a per-row balance; card
    statements do not, and saying "continuous" about a column that does not
    exist would be a claim with nothing behind it.
    """
    with_balance = [t for t in transactions if t.balance_after is not None]
    if opening is None or len(with_balance) < 2:
        report.balance_checked = False
        report.balance_continuous = False
        return

    report.balance_checked = True
    running = opening

    for index, transaction in enumerate(transactions):
        if transaction.balance_after is None:
            # A gap does not break continuity; it just cannot be checked here.
            continue
        expected = running + transaction.signed_amount
        if expected != transaction.balance_after:
            report.balance_continuous = False
            report.first_divergent_row = transaction.source_row if transaction.source_row is not None else index
            report.first_divergent_page = transaction.source_page
            return
        running = transaction.balance_after

    report.balance_continuous = True


def _check_pages(
    report: ReconciliationReport,
    page_count: int | None,
    transactions: Sequence[CanonicalTransaction],
) -> None:
    if not page_count or page_count < 1:
        return
    seen = {t.source_page for t in transactions if t.source_page}
    if not seen:
        return
    # Only pages between the first and last that produced transactions are
    # expected to have them: a statement's final page is often summary only.
    expected = set(range(min(seen), max(seen) + 1))
    missing = sorted(expected - seen)
    report.missing_pages = missing
    report.pages_continuous = not missing
