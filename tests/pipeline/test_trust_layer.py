"""Reconciliation, fingerprinting and confidence — the trust primitives.

These decide whether anything downstream may be believed, so they are tested
against the failures that matter rather than the happy path: a paisa of drift,
a re-issued statement, a genuine same-day repeat, a perfect category on a
misread amount.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.models.enums import CategorySource, Direction, DocumentType, TrustStatus
from app.services import confidence as confidence_service
from app.services import fingerprint as fingerprint_service
from app.services import reconciliation as reconciliation_service

from parsers.canonical import CanonicalTransaction, StatementMetadata


def txn(
    day: int, amount: str, direction: Direction = Direction.DEBIT,
    *, balance: str | None = None, description: str = "UPI-SWIGGY-swiggy@ybl-YESB0-1-PAY",
    merchant_slug: str | None = "swiggy", row: int | None = None,
) -> CanonicalTransaction:
    return CanonicalTransaction(
        txn_date=date(2024, 3, day),
        description=description,
        amount=Decimal(amount),
        direction=direction,
        balance_after=Decimal(balance) if balance else None,
        merchant_slug=merchant_slug,
        source_row=row,
        source_page=1,
    )


def bank_metadata(opening: str, closing: str) -> StatementMetadata:
    return StatementMetadata(
        bank_code="HDFC",
        document_type=DocumentType.BANK_STATEMENT,
        period_start=date(2024, 3, 1),
        period_end=date(2024, 3, 31),
        opening_balance=Decimal(opening),
        closing_balance=Decimal(closing),
    )


class TestReconciliationHasNoToleranceBand:
    def test_a_balanced_statement_reconciles_and_becomes_trusted(self):
        report = reconciliation_service.reconcile(
            bank_metadata("10000.00", "8500.00"),
            [txn(1, "1000.00"), txn(2, "500.00")],
        )
        assert report.reconciles is True
        assert report.delta == Decimal("0.00")
        assert report.trust_status == TrustStatus.TRUSTED

    def test_one_paisa_out_is_untrusted(self):
        """There is no amount of money that is acceptably missing."""
        report = reconciliation_service.reconcile(
            bank_metadata("10000.00", "8500.01"), [txn(1, "1000.00"), txn(2, "500.00")]
        )
        assert report.reconciles is False
        assert report.delta == Decimal("-0.01")
        assert report.trust_status == TrustStatus.UNTRUSTED

    def test_a_statement_without_balances_is_unverified_not_trusted(self):
        """"Nobody checked" and "the arithmetic holds" are different claims."""
        metadata = StatementMetadata(
            bank_code="HDFC", document_type=DocumentType.BANK_STATEMENT
        )
        report = reconciliation_service.reconcile(metadata, [txn(1, "1000.00")])

        assert report.delta is None
        assert report.unverifiable is True
        assert report.reconciles is False
        assert report.trust_status == TrustStatus.PENDING
        assert report.trust_status != TrustStatus.TRUSTED

    def test_a_card_statement_uses_the_card_identity(self):
        """A card bill grows with purchases; a deposit account shrinks."""
        metadata = StatementMetadata(
            bank_code="HDFC",
            document_type=DocumentType.CREDIT_CARD_STATEMENT,
            opening_balance=Decimal("5000.00"),
            total_amount_due=Decimal("6500.00"),
        )
        report = reconciliation_service.reconcile(
            metadata, [txn(3, "2000.00"), txn(5, "500.00", Direction.CREDIT)]
        )
        assert report.reconciles is True

    def test_a_card_refund_read_as_a_purchase_breaks_reconciliation(self):
        """The error is double the refund, and it must not pass silently."""
        metadata = StatementMetadata(
            bank_code="HDFC",
            document_type=DocumentType.CREDIT_CARD_STATEMENT,
            opening_balance=Decimal("5000.00"),
            total_amount_due=Decimal("6500.00"),
        )
        report = reconciliation_service.reconcile(
            metadata, [txn(3, "2000.00"), txn(5, "500.00", Direction.DEBIT)]
        )
        assert report.reconciles is False
        assert report.delta == Decimal("1000.00")


class TestBalanceContinuity:
    def test_a_clean_running_balance_is_continuous(self):
        report = reconciliation_service.reconcile(
            bank_metadata("10000.00", "8500.00"),
            [txn(1, "1000.00", balance="9000.00", row=0),
             txn(2, "500.00", balance="8500.00", row=1)],
        )
        assert report.balance_checked is True
        assert report.balance_continuous is True
        assert report.first_divergent_row is None

    def test_it_names_the_row_where_the_balance_stops_following(self):
        """"Row 1 on page 1" is actionable; "₹300 unaccounted" is not."""
        report = reconciliation_service.reconcile(
            bank_metadata("10000.00", "8500.00"),
            [txn(1, "1000.00", balance="9000.00", row=0),
             txn(2, "500.00", balance="8200.00", row=1)],
        )
        assert report.balance_continuous is False
        assert report.first_divergent_row == 1
        assert report.first_divergent_page == 1

    def test_a_card_statement_claims_nothing_about_continuity(self):
        """There is no per-row balance column, so there is nothing to check."""
        metadata = StatementMetadata(
            bank_code="HDFC",
            document_type=DocumentType.CREDIT_CARD_STATEMENT,
            opening_balance=Decimal("5000.00"),
            total_amount_due=Decimal("7000.00"),
        )
        report = reconciliation_service.reconcile(metadata, [txn(3, "2000.00")])
        assert report.balance_checked is False


class TestFingerprintIdentity:
    TENANT = uuid.uuid4()
    ACCOUNT = uuid.uuid4()

    def _assign(self, transactions):
        return fingerprint_service.assign(
            transactions, tenant_id=self.TENANT, account_id=self.ACCOUNT
        )

    def test_the_same_statement_fingerprints_identically(self):
        """The whole basis of "re-uploading adds zero rows"."""
        rows = [txn(1, "441.00"), txn(2, "1158.00"), txn(3, "99.50")]
        assert self._assign(rows) == self._assign(
            [txn(1, "441.00"), txn(2, "1158.00"), txn(3, "99.50")]
        )

    def test_a_different_balance_does_not_change_identity(self):
        """A re-issued statement carries different balances for the same rows.

        Folding balance into the key would give the identical transaction a
        different hash and silently defeat deduplication — the exact failure
        this ledger must not have.
        """
        original = self._assign([txn(1, "441.00", balance="9000.00")])
        reissued = self._assign([txn(1, "441.00", balance="8250.75")])
        assert original == reissued

    def test_two_genuine_repeats_on_one_day_stay_distinct(self):
        """Two ₹200 Swiggy orders are two transactions.

        Without occurrence numbering the unique constraint would reject the
        second — losing real money from the ledger in order to "protect" it.
        """
        prints = self._assign([txn(5, "200.00"), txn(5, "200.00")])
        assert prints[0] != prints[1]

    def test_repeats_are_numbered_stably_across_re_uploads(self):
        first = self._assign([txn(5, "200.00"), txn(5, "200.00")])
        second = self._assign([txn(5, "200.00"), txn(5, "200.00")])
        assert first == second

    def test_a_different_amount_is_a_different_transaction(self):
        assert self._assign([txn(1, "441.00")]) != self._assign([txn(1, "442.00")])

    def test_a_different_direction_is_a_different_transaction(self):
        assert self._assign([txn(1, "441.00", Direction.DEBIT)]) != self._assign(
            [txn(1, "441.00", Direction.CREDIT)]
        )

    def test_a_different_account_is_a_different_transaction(self):
        mine = self._assign([txn(1, "441.00")])
        theirs = fingerprint_service.assign(
            [txn(1, "441.00")], tenant_id=self.TENANT, account_id=uuid.uuid4()
        )
        assert mine != theirs

    def test_narration_noise_does_not_change_identity(self):
        """The same transaction printed with a different reference number."""
        a = self._assign([txn(1, "441.00", description="UPI-SWIGGY-x@ybl-YESB0-111-PAY")])
        b = self._assign([txn(1, "441.00", description="UPI-SWIGGY-x@ybl-YESB0-999-PAY")])
        assert a == b

    def test_near_duplicates_are_flagged_rather_than_dropped(self):
        similar, score = fingerprint_service.looks_like_near_duplicate(
            txn(1, "441.00", description="UPI-SWIGGY-ORDER PAYMENT"),
            "UPI-SWIGGY-ORDER PAYMENT ",
        )
        assert similar is True and score >= 0.92

    def test_unrelated_descriptions_are_not_near_duplicates(self):
        similar, _ = fingerprint_service.looks_like_near_duplicate(
            txn(1, "441.00", description="UPI-SWIGGY-ORDER PAYMENT"),
            "ATW-4123XXXXXXXX8842-PUNE ATM CASH",
        )
        assert similar is False


class TestConfidenceGate:
    def _score(self, validation: Decimal = Decimal("0.99"), **overrides):
        transaction = txn(1, "441.00")
        transaction.field_confidence = {
            "date": 1.0, "amount": 1.0, "direction": 1.0, "merchant": 0.99,
        }
        transaction.category_source = CategorySource.VERIFIED_MERCHANT_RULE
        transaction.category_slug = "food"
        return confidence_service.score(
            transaction, validation=validation, **overrides
        )

    def test_a_clean_row_is_auto_approved(self):
        scores = self._score()
        assert scores.review_status.value == "auto_approved"
        assert scores.minimum >= confidence_service.AUTO_APPROVE_AT

    def test_the_gate_is_the_minimum_not_the_average(self):
        """The case that justifies the whole design.

        Amount confidence 0.89, everything else near-perfect. The four scores
        average to *exactly* 0.97 — so a blended gate would auto-approve this
        row and put a probably-misread amount into the ledger as settled fact.
        The minimum is 0.89, which sends it to review.
        """
        transaction = txn(1, "441.00")
        transaction.field_confidence = {
            "date": 1.0, "amount": 0.89, "direction": 1.0, "merchant": 1.0,
        }
        transaction.category_source = CategorySource.USER_RULE
        transaction.category_slug = "food"

        scores = confidence_service.score(transaction, validation=Decimal("0.99"))

        average = (
            scores.extraction + scores.merchant + scores.category + scores.validation
        ) / 4
        assert average >= confidence_service.AUTO_APPROVE_AT   # a blend would pass
        assert scores.minimum == Decimal("0.8900")             # the minimum does not
        assert scores.review_status.value == "review_required"
        assert scores.weakest == "extraction"

    def test_a_statement_that_does_not_reconcile_drags_every_row_down(self):
        """Including the rows that look immaculate — the misread might be one."""
        validation = confidence_service.statement_validation_score(
            reconciles=False, unverifiable=False,
            balance_checked=True, balance_continuous=True, pages_continuous=True,
        )
        scores = self._score(validation=validation)
        assert scores.review_status.value == "review_required"

    def test_an_unverifiable_statement_cannot_auto_approve(self):
        validation = confidence_service.statement_validation_score(
            reconciles=False, unverifiable=True,
            balance_checked=False, balance_continuous=False, pages_continuous=True,
        )
        scores = self._score(validation=validation)
        assert scores.review_status.value != "auto_approved"

    def test_an_ocr_row_is_discounted_even_when_nothing_looks_wrong(self):
        plain = self._score()
        scanned = self._score(from_ocr_page=True)
        assert scanned.extraction < plain.extraction

    def test_a_suspected_duplicate_goes_to_review(self):
        scores = self._score(suspected_duplicate=True)
        assert scores.review_status.value == "review_required"

    def test_an_uncategorised_row_is_held_for_review(self):
        transaction = txn(1, "441.00", merchant_slug=None)
        transaction.field_confidence = {"date": 1.0, "amount": 1.0, "merchant": 0.5}
        transaction.category_source = CategorySource.FALLBACK_OTHER
        scores = confidence_service.score(transaction, validation=Decimal("0.99"))
        assert scores.review_status.value == "review_required"

    @pytest.mark.parametrize(
        ("minimum", "expected"),
        [
            (Decimal("0.99"), "auto_approved"),
            (Decimal("0.97"), "auto_approved"),
            (Decimal("0.96"), "flagged"),
            (Decimal("0.90"), "flagged"),
            (Decimal("0.89"), "review_required"),
        ],
    )
    def test_the_published_bands(self, minimum, expected):
        scores = confidence_service.Confidence(
            extraction=minimum, merchant=Decimal("1"),
            category=Decimal("1"), validation=Decimal("1"),
        )
        assert scores.review_status.value == expected
