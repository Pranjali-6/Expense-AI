"""Every golden fixture, parsed and checked against its ground truth.

These are regression tests over the same corpus ``make accuracy`` scores, but
they assert *exactness* per fixture rather than aggregate rates: a fixture that
loses one transaction fails here immediately and names itself, instead of being
diluted into a 99.86% that still clears the gate.

Note what is being proved and what is not. The fixtures are synthetic and were
authored alongside the parsers, so a green run says the framework is correct
against layouts we wrote — not that it reads a real HDFC statement. P4.5 exists
for that, and until a real corpus is supplied the claim stays scoped to this.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.models.enums import Direction, DocumentType

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "statements"


def _fixture_ids() -> list[str]:
    return sorted(path.stem for path in FIXTURES.glob("*.pdf")) if FIXTURES.exists() else []


pytestmark = pytest.mark.skipif(
    not _fixture_ids(), reason="run `make gen-fixtures` first"
)


@pytest.fixture(scope="module")
def parsed() -> dict[str, tuple[dict, object]]:
    """Parse every fixture once; the OCR one is slow."""
    from app.extraction.pipeline import parse_document

    results: dict[str, tuple[dict, object]] = {}
    for pdf in sorted(FIXTURES.glob("*.pdf")):
        truth = json.loads(pdf.with_name(f"{pdf.stem}.expected.json").read_text())
        results[pdf.stem] = (truth, parse_document(pdf.read_bytes()))
    return results


@pytest.mark.slow
@pytest.mark.parametrize("name", _fixture_ids())
class TestEveryFixture:
    def test_every_transaction_is_found_and_none_invented(self, parsed, name):
        truth, outcome = parsed[name]
        assert len(outcome.result.transactions) == len(truth["transactions"]), (
            f"{name}: expected {len(truth['transactions'])} transactions, "
            f"got {len(outcome.result.transactions)}"
        )

    def test_the_statement_reconciles_to_exactly_zero(self, parsed, name):
        """No tolerance band. The arithmetic closes or the statement is not
        trusted."""
        from tools.accuracy_harness.scoring import reconcile_bank, reconcile_card

        _, outcome = parsed[name]
        metadata = outcome.result.metadata

        if metadata.document_type == DocumentType.CREDIT_CARD_STATEMENT:
            ok, delta = reconcile_card(
                metadata.opening_balance, metadata.total_amount_due,
                outcome.result.transactions,
            )
        else:
            ok, delta = reconcile_bank(
                metadata.opening_balance, metadata.closing_balance,
                outcome.result.transactions,
            )

        assert delta is not None, f"{name}: not enough printed to check the arithmetic"
        assert delta == Decimal("0.00"), f"{name}: off by {delta}"
        assert ok is True

    def test_the_right_bank_was_detected(self, parsed, name):
        truth, outcome = parsed[name]
        assert outcome.result.metadata.bank_code == truth["metadata"]["bank_code"]

    def test_the_document_type_was_classified(self, parsed, name):
        truth, outcome = parsed[name]
        assert str(outcome.document_type) == truth["metadata"]["document_type"]

    def test_the_statement_period_was_read(self, parsed, name):
        truth, outcome = parsed[name]
        metadata = outcome.result.metadata
        assert metadata.period_start.isoformat() == truth["metadata"]["period_start"]
        assert metadata.period_end.isoformat() == truth["metadata"]["period_end"]

    def test_only_the_last_four_account_digits_are_kept(self, parsed, name):
        """A parser that captured a full account number would be creating the
        identifier everything downstream works to never store."""
        truth, outcome = parsed[name]
        last4 = outcome.result.metadata.account_last4
        assert last4 == truth["metadata"]["account_last4"]
        assert last4 is None or len(last4) == 4

    def test_amounts_are_exact_decimals_never_floats(self, parsed, name):
        _, outcome = parsed[name]
        for transaction in outcome.result.transactions:
            assert isinstance(transaction.amount, Decimal)
            assert transaction.amount > 0        # sign lives in `direction`
            assert transaction.amount == transaction.amount.quantize(Decimal("0.01"))

    def test_page_furniture_is_not_read_as_transactions(self, parsed, name):
        """B/F, C/F, Opening Balance and Total lines carry balance-shaped
        payloads. Reading one adds a phantom transaction per page."""
        _, outcome = parsed[name]
        for transaction in outcome.result.transactions:
            upper = transaction.description.upper()
            assert not upper.startswith(("B/F", "C/F", "OPENING BALANCE",
                                         "CLOSING BALANCE", "TOTAL"))


class TestSpecificHazards:
    def test_a_dr_cr_suffix_layout_is_not_inverted(self, parsed):
        """Kotak prints one amount column with a Dr/Cr suffix. Ignoring the
        suffix reads every credit as a debit."""
        truth, outcome = parsed["kotak-2024-03"]
        credits = [
            t for t in outcome.result.transactions if t.direction == Direction.CREDIT
        ]
        expected_credits = [
            t for t in truth["transactions"] if t["direction"] == "credit"
        ]
        assert len(credits) == len(expected_credits) > 0

    def test_a_card_refund_is_a_credit_not_a_purchase(self, parsed):
        truth, outcome = parsed["icici-card-2024-03"]
        credits = [
            t for t in outcome.result.transactions if t.direction == Direction.CREDIT
        ]
        assert credits, "the card fixture contains credits and none were read"
        # Reading a refund as a purchase adds twice its value to the month.
        assert len(credits) == sum(
            1 for t in truth["transactions"] if t["direction"] == "credit"
        )

    def test_lakh_grouped_amounts_survive(self, parsed):
        truth, outcome = parsed["hdfc-2024-04-lakh"]
        largest = max(t.amount for t in outcome.result.transactions)
        expected = max(Decimal(t["amount"]) for t in truth["transactions"])
        assert largest == expected
        assert largest > Decimal("100000")

    def test_page_breaks_do_not_lose_or_duplicate_rows(self, parsed):
        """Six pages, five brought-forward and five carried-forward lines."""
        truth, outcome = parsed["axis-2024-04-multipage"]
        assert truth["page_count"] >= 5
        assert len(outcome.result.transactions) == len(truth["transactions"])
        pages = {t.source_page for t in outcome.result.transactions}
        assert len(pages) >= 5, "transactions were not attributed across pages"

    def test_a_reissued_statement_yields_identical_transactions(self, parsed):
        """The input to P5's duplicate detection: same content, different PDF."""
        _, first = parsed["hdfc-2024-03"]
        _, reissue = parsed["hdfc-2024-03-reissued"]

        def key(transactions):
            return sorted(
                (t.txn_date, t.amount, str(t.direction), t.description)
                for t in transactions
            )

        assert key(first.result.transactions) == key(reissue.result.transactions)

    def test_a_scanned_statement_is_read_by_ocr_and_still_reconciles(self, parsed):
        """OCR misreads digits. The guarantee is not that it never does — it is
        that a misread statement fails reconciliation instead of being trusted.
        This fixture happens to reconcile; the assertion that matters is that it
        was genuinely read through the OCR path."""
        from app.models.enums import ExtractionMethod
        from tools.accuracy_harness.scoring import reconcile_bank

        truth, outcome = parsed["sbi-2024-04-scanned"]
        assert outcome.document.method == ExtractionMethod.OCR
        assert outcome.ocr_page_count == truth["page_count"]

        _, delta = reconcile_bank(
            outcome.result.metadata.opening_balance,
            outcome.result.metadata.closing_balance,
            outcome.result.transactions,
        )
        assert delta == Decimal("0.00")
