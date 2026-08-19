"""Merchant identification and the deterministic category rules.

Together these are the reason the platform categorises an Indian statement
correctly with ``AI_ENABLED=false``. They are also where the privacy boundary
starts: ``is_known`` marks a name as coming from the seeded dictionary, and the
gateway in P6 refuses to send anything else to a model.
"""

from __future__ import annotations

import pytest

from app.models.enums import MovementType, PaymentMethod

from parsers.merchants.normalizer import detect_payment_method, normalize_merchant
from parsers.merchants.rules import match_rule


class TestOneMerchantAcrossEveryBanksNarrationStyle:
    """The same Swiggy payment, as five different banks print it."""

    @pytest.mark.parametrize(
        "narration",
        [
            "UPI-SWIGGY-SWIGGY@YBL-YESB0YBLUPI-412345678901-PAYMENT",
            "UPI/412345678901/Payment/swiggy@ybl/YES BANK",
            "TO TRANSFER-UPI/DR/412345678901/SWIGGY/YESB/swiggy@ybl/Payment",
            "UPI/P2M/412345678901/SWIGGY/YES BANK",
            "UPI-412345678901-SWIGGY@YBL-PAYMENT TO SWIGGY",
            "POS 4123XXXXXXXX8842 SWIGGY               BANGALORE IN",
            "NEFT-CITIN52410318-BUNDL TECHNOLOGIES PVT LTD",
        ],
    )
    def test_all_of_them_resolve_to_one_merchant(self, narration):
        match = normalize_merchant(narration)
        assert match.name == "Swiggy"
        assert match.slug == "swiggy"
        assert match.is_known is True
        assert match.category_slug == "food"

    def test_a_more_specific_alias_wins(self):
        """SWIGGY INSTAMART is groceries, not restaurant food."""
        match = normalize_merchant("UPI-SWIGGY INSTAMART-instamart@ybl-YESB0-99-PAYMENT")
        assert match.slug == "instamart"
        assert match.category_slug == "grocery"


class TestRailDetection:
    @pytest.mark.parametrize(
        ("narration", "expected"),
        [
            ("UPI-SWIGGY-x@ybl-YESB0-1-PAYMENT", PaymentMethod.UPI),
            ("POS 4123XXXXXXXX8842 DMART", PaymentMethod.CARD),
            ("ATW-4123XXXXXXXX8842-BANGALORE-HDFC", PaymentMethod.ATM),
            ("NEFT DR-HDFC0000123-SOMEONE-123", PaymentMethod.NEFT),
            ("IMPS-412312345678-SOMEONE-HDFC", PaymentMethod.IMPS),
            ("RTGS-UTIB0000123-SOMEONE", PaymentMethod.RTGS),
            ("ACH D- NETFLIX-123456", PaymentMethod.ACH),
            ("NACH/SPOTIFY/123456", PaymentMethod.NACH),
            ("CHQ PAID-MICR CTS-000123-SOMEONE", PaymentMethod.CHEQUE),
        ],
    )
    def test_the_leading_token_names_the_rail(self, narration, expected):
        assert detect_payment_method(narration) == expected


class TestRowsWithNoMerchant:
    """An ATM withdrawal has a rail and an amount and no business.

    Emitting the leftover words as a merchant creates junk in analytics and
    hands the privacy gateway an unverified string to police.
    """

    @pytest.mark.parametrize(
        "narration",
        [
            "ATW-4123XXXXXXXX8842-BANGALORE-HDFC BANK",
            "BY CASH WDL-ATM 4123XXXXXXXX8842 PUNE",
            "NWD-4123XXXXXXXX8842-MUMBAI",
            "SMS ALERT CHARGES-MIR2419000001",
            "TO TRANSFER-CREDIT INTEREST CAPITALISED",
            "GST ON FINANCE CHARGES",
            "TO TRANSFER-INB CREDIT CARD PAYMENT 123456789012",
            "PAYMENT RECEIVED - THANK YOU",
        ],
    )
    def test_no_merchant_is_invented(self, narration):
        from parsers.base import BankParser
        from parsers.canonical import CanonicalTransaction
        from datetime import date
        from decimal import Decimal
        from app.models.enums import Direction

        txn = CanonicalTransaction(
            txn_date=date(2024, 3, 1), description=narration,
            amount=Decimal("100.00"), direction=Direction.DEBIT,
        )
        BankParser.enrich([txn])
        assert txn.merchant_normalized is None


class TestPersonToPersonTransfers:
    def test_a_persons_name_is_returned_but_never_marked_known(self):
        """The flag the privacy gateway keys off.

        A P2P transfer's counterparty is a person. Storing the name is fine —
        it is the user's own record — but it must never be sent anywhere as a
        "merchant", and `is_known=False` is what makes that decidable.
        """
        match = normalize_merchant("IMPS-412312345678-RAHUL SHARMA-HDFC-XXXXXX1234")
        assert match.name == "Rahul Sharma"
        assert match.is_known is False
        assert match.slug is None

    def test_rail_tokens_do_not_become_part_of_the_name(self):
        for narration in (
            "IMPS/P2A/412312345678/RAHUL SHARMA",
            "MB IMPS-412312345678-RAHUL SHARMA",
            "BY TRANSFER-NEFT*8299*RAHUL SHARMA",
        ):
            assert normalize_merchant(narration).name == "Rahul Sharma"


class TestFuzzyMatchingIsHonest:
    def test_a_fuzzy_match_never_reaches_dictionary_confidence(self):
        exact = normalize_merchant("UPI-SWIGGY-swiggy@ybl-YESB0-1-PAYMENT")
        assert exact.confidence == 0.99

    def test_a_similar_looking_unrelated_word_is_not_matched(self):
        """Short candidates share letters easily; ZEPHYR is not Zepto."""
        match = normalize_merchant("UPI-ZEPHYR CONSULTING-x@ybl-YESB0-1-PAYMENT")
        assert match.slug != "zepto"


class TestDeterministicRules:
    def test_a_refund_outranks_the_merchants_own_category(self):
        """Structure beats identity: an Amazon refund is a refund, not shopping."""
        from parsers.base import BankParser
        from parsers.canonical import CanonicalTransaction
        from datetime import date
        from decimal import Decimal
        from app.models.enums import Direction

        txn = CanonicalTransaction(
            txn_date=date(2024, 3, 1),
            description="UPI-AMAZON-amazon@apl-YESB0-1-PAYMENT-REFUND",
            amount=Decimal("500.00"), direction=Direction.CREDIT,
        )
        BankParser.enrich([txn])

        assert txn.merchant_normalized == "Amazon"   # merchant is still Amazon
        assert txn.category_slug == "refund"         # category is not shopping
        assert txn.is_expense is False

    @pytest.mark.parametrize(
        ("narration", "category", "is_expense"),
        [
            ("TO TRANSFER-INB CREDIT CARD PAYMENT 123", "credit_card_payment", False),
            ("BIL/ONL/012596627324/ICICI CREDIT CARD", "credit_card_payment", False),
            ("ATW-4123XXXXXXXX8842-PUNE-HDFC", "cash_withdrawal", False),
            ("NEFT CR-CITI0000004-ACME LTD-SALARY-123", "salary", False),
            ("ACH D- HDB FINANCIAL SERVICES-8534", "emi", True),
            ("SMS ALERT CHARGES-MIR2419000001", "bank_charges", True),
            ("IMPS-4123-RAHUL SHARMA RENT-HDFC", "rent", True),
            ("UPI/123/SIP MUTUAL FUND/groww@ybl", "investment", False),
        ],
    )
    def test_structural_categories_are_recognised_without_a_model(
        self, narration, category, is_expense
    ):
        rule = match_rule(narration)
        assert rule is not None, narration
        assert rule.rule.category_slug == category
        assert rule.rule.is_expense is is_expense

    def test_money_moved_between_your_own_accounts_is_not_spending(self):
        """The classic way a finance app reports double someone's spending."""
        for narration in (
            "TO TRANSFER-INB CREDIT CARD PAYMENT 123",
            "ATW-4123XXXXXXXX8842-PUNE",
            "UPI/123/SELF TRANSFER/x@ybl",
        ):
            rule = match_rule(narration)
            assert rule is not None and rule.rule.is_expense is False

    def test_an_unannotated_transfer_to_a_person_is_not_guessed_as_rent(self):
        """Nothing in the document says it is rent, so nothing may claim it is."""
        assert match_rule("IMPS-412312345678-RAHUL SHARMA-HDFC-XXXXXX1234") is None

    def test_a_matched_rule_records_why(self):
        from parsers.base import BankParser
        from parsers.canonical import CanonicalTransaction
        from datetime import date
        from decimal import Decimal
        from app.models.enums import CategorySource, Direction

        txn = CanonicalTransaction(
            txn_date=date(2024, 3, 1), description="ATW-4123XXXXXXXX8842-PUNE",
            amount=Decimal("2000.00"), direction=Direction.DEBIT,
        )
        BankParser.enrich([txn])

        assert txn.category_source == CategorySource.DETERMINISTIC_RULE
        assert txn.category_reason["rule"] == "cash_withdrawal"
        assert txn.movement_type == MovementType.CASH_WITHDRAWAL
