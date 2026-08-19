"""Mutation tests for the accuracy harness.

A green scorecard is only worth something if the scorer can go red. These tests
deliberately corrupt a known-good extraction and assert that the harness
notices — and, just as importantly, that it notices in the *right category*.

The failure this guards against is specific and is the reason the harness was
specified the way it was: a scorer that computes field accuracy over the rows it
managed to extract will happily report 100% amount accuracy on a statement it
read half of. Every metric here divides by the ground-truth count, and a missing
transaction counts as a failure in every field metric. `test_dropping_rows_...`
is the test that pins that down.
"""

from __future__ import annotations

import copy
from datetime import timedelta
from decimal import Decimal

import pytest

from app.models.enums import Direction

from parsers.canonical import CanonicalTransaction
from tools.accuracy_harness.scoring import (
    GroundTruth,
    reconcile_bank,
    reconcile_card,
    score,
)


def _truth(index: int, *, amount: str, direction: Direction = Direction.DEBIT) -> GroundTruth:
    from datetime import date

    return GroundTruth(
        txn_date=date(2024, 3, 1) + timedelta(days=index),
        description=f"UPI-MERCHANT{index}-M{index}@YBL-YESB0-4123456789{index:02d}-PAYMENT",
        amount=Decimal(amount),
        direction=direction,
        merchant=f"Merchant{index}",
        category_slug="food",
        balance_after=None,
    )


def _extracted(truth: GroundTruth, row: int) -> CanonicalTransaction:
    return CanonicalTransaction(
        txn_date=truth.txn_date,
        description=truth.description,
        amount=truth.amount,
        direction=truth.direction,
        merchant_normalized=truth.merchant,
        category_slug=truth.category_slug,
        source_page=1,
        source_row=row,
    )


@pytest.fixture
def perfect() -> tuple[list[GroundTruth], list[CanonicalTransaction]]:
    expected = [_truth(index, amount=f"{100 + index * 7}.50") for index in range(20)]
    extracted = [_extracted(truth, row) for row, truth in enumerate(expected)]
    return expected, extracted


def _score(expected, extracted):
    return score(
        fixture="synthetic-test", bank_code="TEST", document_type="bank_statement",
        expected=expected, extracted=extracted,
    )


class TestTheHarnessRecognisesAPerfectRun:
    def test_identical_input_scores_100_percent(self, perfect):
        card = _score(*perfect)

        assert card.missing == 0
        assert card.extra == 0
        assert card.matched_count == 20
        assert card.recall == 1.0
        assert card.precision == 1.0
        assert card.amount_accuracy == 1.0
        assert card.merchant_accuracy == 1.0


class TestDroppedTransactions:
    def test_a_dropped_row_lowers_recall(self, perfect):
        expected, extracted = perfect
        card = _score(expected, extracted[:-3])

        assert card.missing == 3
        assert card.recall == pytest.approx(17 / 20)

    def test_dropping_rows_cannot_flatter_the_field_metrics(self, perfect):
        """The rule the whole harness exists to enforce.

        Half the statement is dropped. Every row that *was* read is perfect. A
        naive scorer would report 100% on amount, date and direction — because
        it only ever looks at what it extracted. Here every field metric must
        fall to 50%, because the denominator is ground truth.
        """
        expected, extracted = perfect
        card = _score(expected, extracted[:10])

        assert card.missing == 10
        assert card.recall == 0.5
        # Not one of these may report perfection.
        assert card.amount_accuracy == 0.5
        assert card.date_accuracy == 0.5
        assert card.direction_accuracy == 0.5
        assert card.merchant_accuracy == 0.5
        assert card.category_accuracy == 0.5

    def test_extracting_nothing_scores_zero_not_undefined(self, perfect):
        expected, _ = perfect
        card = _score(expected, [])

        assert card.missing == 20
        assert card.recall == 0.0
        assert card.amount_accuracy == 0.0
        assert card.precision == 0.0


class TestPhantomTransactions:
    def test_an_invented_row_lowers_precision(self, perfect):
        expected, extracted = perfect
        phantom = _extracted(_truth(99, amount="9999.99"), row=99)
        card = _score(expected, [*extracted, phantom])

        assert card.extra == 1
        assert card.precision == pytest.approx(20 / 21)
        # Recall is untouched: nothing was lost, something was invented. The two
        # failures are reported separately and never averaged together.
        assert card.recall == 1.0

    def test_a_duplicated_row_counts_as_extra(self, perfect):
        expected, extracted = perfect
        card = _score(expected, [*extracted, copy.deepcopy(extracted[0])])

        assert card.extra == 1
        assert card.missing == 0


class TestFieldErrors:
    def test_a_wrong_amount_is_an_amount_error_not_a_missing_row(self, perfect):
        """A misread row was still read. Counting it as missing *and* extra
        would double-count one defect and hide what actually went wrong."""
        expected, extracted = perfect
        extracted[4].amount = Decimal("999.99")
        card = _score(expected, extracted)

        assert card.missing == 0
        assert card.extra == 0
        assert card.errors.amount == 1
        assert card.amount_accuracy == pytest.approx(19 / 20)
        assert card.recall == 1.0

    def test_a_flipped_direction_is_caught(self, perfect):
        expected, extracted = perfect
        extracted[7].direction = Direction.CREDIT
        card = _score(expected, extracted)

        assert card.errors.direction == 1
        assert card.direction_accuracy == pytest.approx(19 / 20)

    def test_a_shifted_date_is_caught(self, perfect):
        expected, extracted = perfect
        extracted[2].txn_date = extracted[2].txn_date + timedelta(days=1)
        card = _score(expected, extracted)

        assert card.errors.date == 1
        assert card.date_accuracy == pytest.approx(19 / 20)

    def test_a_wrong_merchant_is_caught(self, perfect):
        expected, extracted = perfect
        extracted[3].merchant_normalized = "Something Else"
        card = _score(expected, extracted)

        assert card.errors.merchant == 1

    def test_merchant_comparison_ignores_case_only(self, perfect):
        expected, extracted = perfect
        extracted[3].merchant_normalized = extracted[3].merchant_normalized.upper()
        card = _score(expected, extracted)

        assert card.errors.merchant == 0

    def test_a_dropped_merchant_is_not_silently_forgiven(self, perfect):
        expected, extracted = perfect
        extracted[3].merchant_normalized = None
        card = _score(expected, extracted)

        assert card.errors.merchant == 1


class TestReconciliation:
    def test_a_balanced_statement_reconciles_to_exactly_zero(self):
        transactions = [
            _extracted(_truth(0, amount="1000.00", direction=Direction.DEBIT), 0),
            _extracted(_truth(1, amount="250.50", direction=Direction.CREDIT), 1),
        ]
        ok, delta = reconcile_bank(Decimal("5000.00"), Decimal("4250.50"), transactions)

        assert ok is True
        assert delta == Decimal("0.00")

    def test_one_paisa_out_is_not_reconciled(self):
        """There is no tolerance band, and this is the test that says so."""
        transactions = [
            _extracted(_truth(0, amount="1000.00", direction=Direction.DEBIT), 0),
        ]
        ok, delta = reconcile_bank(Decimal("5000.00"), Decimal("4000.01"), transactions)

        assert ok is False
        assert delta == Decimal("-0.01")

    def test_a_missing_balance_is_unverified_not_verified(self):
        """The distinction the trust status is built on."""
        ok, delta = reconcile_bank(None, Decimal("4000.00"), [])

        assert ok is False
        assert delta is None

    def test_card_reconciliation_uses_the_card_identity(self):
        transactions = [
            _extracted(_truth(0, amount="2000.00", direction=Direction.DEBIT), 0),
            _extracted(_truth(1, amount="500.00", direction=Direction.CREDIT), 1),
        ]
        ok, delta = reconcile_card(Decimal("10000.00"), Decimal("11500.00"), transactions)

        assert ok is True
        assert delta == Decimal("0.00")

    def test_a_card_refund_read_as_a_purchase_breaks_reconciliation(self):
        """Doubles the error rather than losing it — and must be caught."""
        transactions = [
            _extracted(_truth(0, amount="2000.00", direction=Direction.DEBIT), 0),
            _extracted(_truth(1, amount="500.00", direction=Direction.DEBIT), 1),
        ]
        ok, delta = reconcile_card(Decimal("10000.00"), Decimal("11500.00"), transactions)

        assert ok is False
        assert delta == Decimal("1000.00")
