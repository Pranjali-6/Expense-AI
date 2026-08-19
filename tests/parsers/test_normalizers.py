"""Amount, date and narration normalization.

These are the smallest units in the extraction stack and the ones where a bug is
least visible. A month shifted by a day-first/month-first mixup or a `Dr` suffix
ignored does not raise anything — it just makes every downstream number quietly
wrong.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.models.enums import Direction

from parsers.normalizers import dates as datenorm
from parsers.normalizers import text as textnorm
from parsers.normalizers.amount import (
    AmountParseError,
    parse_amount,
    parse_amount_with_direction,
)


class TestIndianAmountFormats:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1,23,456.78", "123456.78"),   # lakh grouping, not 123,456.78
            ("12,34,56,789.00", "123456789.00"),  # crore
            ("1,234.56", "1234.56"),
            ("999.99", "999.99"),
            ("0.01", "0.01"),
            ("₹1,23,456.78", "123456.78"),
            ("Rs. 4,500.00", "4500.00"),
            ("INR 4,500", "4500.00"),
            ("2,22 ,838.23", "222838.23"),  # OCR's stray space beside a separator
        ],
    )
    def test_grouping_and_currency_noise(self, raw, expected):
        assert parse_amount(raw) == Decimal(expected)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1,234.56 Dr", "1234.56"),
            ("1,234.56 Cr", "1234.56"),
            ("-1,234.56", "-1234.56"),
            ("1,234.56-", "-1234.56"),
            ("(1,234.56)", "-1234.56"),   # accounting negative
        ],
    )
    def test_sign_markers(self, raw, expected):
        assert parse_amount(raw) == Decimal(expected)

    def test_amounts_are_exact_not_floating_point(self):
        # The reason money never touches float: 0.1 + 0.2 != 0.3 in binary.
        total = parse_amount("0.10") + parse_amount("0.20")
        assert total == Decimal("0.30")
        assert str(total) == "0.30"

    @pytest.mark.parametrize("raw", ["", "   ", "-", "N/A", "NIL", None, "abc"])
    def test_unreadable_cells_raise_rather_than_guess(self, raw):
        with pytest.raises(AmountParseError):
            parse_amount(raw)

    def test_the_error_carries_no_cell_content(self):
        """Parse errors reach the logs, which may not carry financial data."""
        with pytest.raises(AmountParseError) as caught:
            parse_amount("12,34,567.89 GARBAGE HERE")
        assert "12" not in str(caught.value)
        assert "GARBAGE" not in str(caught.value)


class TestSingleColumnDirection:
    def test_a_cr_suffix_means_credit(self):
        amount, direction = parse_amount_with_direction("1,234.56 Cr")
        assert (amount, direction) == (Decimal("1234.56"), Direction.CREDIT)

    def test_a_dr_suffix_means_debit(self):
        amount, direction = parse_amount_with_direction("1,234.56 Dr")
        assert (amount, direction) == (Decimal("1234.56"), Direction.DEBIT)

    def test_a_leading_minus_means_debit(self):
        amount, direction = parse_amount_with_direction("-1,234.56")
        assert (amount, direction) == (Decimal("1234.56"), Direction.DEBIT)

    def test_an_unmarked_cell_refuses_to_guess_by_default(self):
        """A bank statement always prints direction. Guessing inverts rows."""
        with pytest.raises(AmountParseError):
            parse_amount_with_direction("1,234.56")

    def test_an_unmarked_cell_uses_the_declared_default_when_given(self):
        """Card statements genuinely print purchases unmarked."""
        amount, direction = parse_amount_with_direction(
            "1,234.56", default=Direction.DEBIT
        )
        assert (amount, direction) == (Decimal("1234.56"), Direction.DEBIT)

    def test_the_word_credit_inside_a_narration_is_not_a_cr_marker(self):
        # "CREDIT CARD PAYMENT" must not read as a credit.
        with pytest.raises(AmountParseError):
            parse_amount_with_direction("CREDIT CARD PAYMENT")


class TestDayFirstDates:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("05/03/2024", date(2024, 3, 5)),
            ("05-03-2024", date(2024, 3, 5)),
            ("05.03.2024", date(2024, 3, 5)),
            ("05/03/24", date(2024, 3, 5)),
            ("05 Mar 2024", date(2024, 3, 5)),
            ("05-Mar-2024", date(2024, 3, 5)),
            ("05-Mar-24", date(2024, 3, 5)),
            ("2024-03-05", date(2024, 3, 5)),
            ("Mar 05, 2024", date(2024, 3, 5)),
        ],
    )
    def test_every_indian_format_resolves_to_the_same_day(self, raw, expected):
        assert datenorm.parse_date(raw) == expected

    def test_ambiguous_dates_are_day_first(self):
        """05/03/2024 is 5 March in India and 3 May to a US-locale parser.

        Reading it month-first does not fail — it silently moves a third of
        every statement into the wrong month.
        """
        assert datenorm.parse_date("05/03/2024") == date(2024, 3, 5)
        assert datenorm.parse_date("12/01/2024") == date(2024, 1, 12)

    def test_a_year_hint_fills_in_a_missing_year(self):
        assert datenorm.parse_date("12 Mar", year_hint=2024) == date(2024, 3, 12)

    def test_a_year_hint_never_overrides_a_printed_year(self):
        assert datenorm.parse_date("12 Mar 2019", year_hint=2024) == date(2019, 3, 12)

    def test_unparseable_dates_raise(self):
        with pytest.raises(datenorm.DateParseError):
            datenorm.parse_date("not a date")

    def test_leading_date_detects_a_transaction_row(self):
        assert datenorm.leading_date("05/03/24 UPI-SWIGGY-... 441.00") == "05/03/24"

    def test_leading_date_ignores_a_header_line_that_merely_mentions_one(self):
        """"Statement From 01/03/2024" is not a transaction row."""
        assert datenorm.leading_date("Statement From 01/03/2024 to 31/03/2024") is None

    def test_leading_date_ignores_a_wrapped_continuation_line(self):
        assert datenorm.leading_date("   PAYMENT TO MERCHANT CONTINUED") is None


class TestNarrationCleanup:
    def test_wrapped_cells_are_joined_with_a_space_not_welded(self):
        assert textnorm.collapse("UPI-SWIGGY-\nSWIGGY@YBL") == "UPI-SWIGGY- SWIGGY@YBL"

    def test_masked_cards_and_long_references_are_stripped(self):
        cleaned = textnorm.strip_machine_noise(
            "POS 4256XXXXXXXX3110 STARBUCKS 017507866546"
        )
        assert "4256XXXXXXXX3110" not in cleaned
        assert "017507866546" not in cleaned
        assert "STARBUCKS" in cleaned

    def test_ifsc_codes_are_stripped(self):
        assert "HDFC0269204" not in textnorm.strip_machine_noise(
            "NEFT DR-HDFC0269204-SOMEONE"
        )

    def test_a_upi_handle_yields_its_local_part(self):
        assert textnorm.upi_handle_name("UPI-x-swiggy@ybl-y") == "swiggy"

    def test_a_phone_number_vpa_is_rejected_as_a_merchant_name(self):
        """A numeric VPA identifies a person, and must never become a merchant."""
        assert textnorm.upi_handle_name("UPI-x-9876543210@ybl-y") is None
