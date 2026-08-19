"""The check that makes the assistant safe to believe.

These tests are written the way the check has to work to be worth having: the
positive cases prove it does not reject correct answers (a check that fires on
everything gets switched off), and the negative cases prove it catches the
failure it exists for — a figure that reads perfectly and came from nowhere.

The canonical negative is *derivation*, not fabrication. A model handed two
category totals and asked for their sum will produce the right answer most of
the time. "Most of the time" is the problem: nothing downstream can tell which
time it is, so the answer is rejected whether or not the arithmetic was right.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.assistant.traceability import allowed_figures, check

VIEW = {
    "month": "2024-03",
    "month_label": "March 2024",
    "spending_rupees": 24010,
    "income_rupees": 85000,
    "savings_rate_percent": 45,
    "transaction_count": 12,
    "categories": [
        {"category": "Food", "amount_rupees": 9502, "share_percent": 40},
        {"category": "Grocery", "amount_rupees": 5400, "share_percent": 22},
    ],
}


class TestASoundAnswerPasses:
    @pytest.mark.parametrize(
        "answer",
        [
            "In March 2024 you spent ₹24,010.",
            "Food was your largest category at ₹9,502, which is 40% of spending.",
            "You kept 45% of what you earned, on ₹85,000 of income.",
            "There were 12 transactions in March 2024.",
            "Grocery came to ₹5,400 across the month.",
        ],
    )
    def test_figures_present_in_the_tool_result(self, answer):
        assert check(answer, allowed_figures(VIEW)).ok

    def test_a_figure_without_separators_is_the_same_figure(self):
        assert check("You spent ₹24010.", allowed_figures(VIEW)).ok

    def test_trailing_paise_do_not_break_equality(self):
        """₹9,502.00 and 9502 are one number, not two."""
        assert check("Food came to ₹9,502.00.", allowed_figures(VIEW)).ok

    def test_a_year_is_phrasing_rather_than_a_quantity(self):
        assert check("Spending in the 2019 financial year.", allowed_figures({})).ok

    def test_prose_with_no_figures_at_all_passes(self):
        assert check("Nothing stands out this month.", allowed_figures(VIEW)).ok

    def test_the_word_one_is_not_treated_as_a_count(self):
        """It is a pronoun far more often than a number."""
        assert check("Food was the largest one.", allowed_figures(VIEW)).ok

    def test_a_figure_quoted_inside_a_reason_string_is_traceable(self):
        view = {"outliers": [{"reason": "₹3,692.20 at DMart is 4.2× the usual"}]}
        assert check("A ₹3,692.20 charge was 4.2 times the usual.", allowed_figures(view)).ok


class TestAnInventedFigureIsCaught:
    def test_a_derived_sum_is_rejected(self):
        """The canonical failure: two real numbers, one invented total."""
        outcome = check(
            "Food and groceries together came to ₹14,902.", allowed_figures(VIEW)
        )
        assert not outcome.ok
        assert outcome.kinds == ("currency",)

    def test_a_fabricated_amount_is_rejected(self):
        outcome = check("You spent ₹31,000 in March 2024.", allowed_figures(VIEW))
        assert not outcome.ok

    def test_a_computed_percentage_is_rejected(self):
        outcome = check("Groceries were 63% of your spending.", allowed_figures(VIEW))
        assert not outcome.ok
        assert outcome.kinds == ("percentage",)

    def test_a_lakh_conversion_is_rejected(self):
        """A converted figure is a derived figure, however friendly it reads."""
        outcome = check("You spent about ₹0.24 lakh.", allowed_figures(VIEW))
        assert not outcome.ok

    def test_a_wrong_count_is_rejected(self):
        outcome = check("There were 47 transactions.", allowed_figures(VIEW))
        assert not outcome.ok

    def test_a_wrong_word_number_is_rejected(self):
        outcome = check("You have seventeen subscriptions.", allowed_figures(VIEW))
        assert not outcome.ok
        assert outcome.kinds == ("word_number",)

    def test_a_year_shaped_amount_is_not_exempt(self):
        """The exemption is for bare years, never for anything written as money."""
        outcome = check("You spent ₹2,024 on fuel.", allowed_figures(VIEW))
        assert not outcome.ok
        assert outcome.kinds == ("currency",)

    def test_findings_name_the_kind_without_repeating_the_figure(self):
        """`kinds` is what reaches a log; the figure is the user's own money."""
        outcome = check("You spent ₹31,000.", allowed_figures(VIEW))
        assert outcome.kinds == ("currency",)
        assert all(finding.kind for finding in outcome.findings)


class TestTheAllowedSet:
    def test_numbers_come_from_values_and_from_strings(self):
        allowed = allowed_figures({"total_rupees": 500, "date": "2024-03-14"})
        assert Decimal(500) in allowed.currency
        assert Decimal(2024) in allowed.plain
        assert Decimal(14) in allowed.plain

    def test_booleans_are_not_numbers(self):
        """`True` is `1` in Python, and would silently license the figure 1."""
        assert Decimal(1) not in allowed_figures({"fully_trusted": True}).any_kind

    def test_arguments_are_a_legitimate_source(self):
        """A `limit` the model chose is a figure it may quote back."""
        assert check("Here are the top 5.", allowed_figures({"limit": 5})).ok


class TestKindsAreNotInterchangeable:
    """The weakness a flat set of numbers had, tested from both sides."""

    def test_a_percentage_does_not_license_a_rupee_figure(self):
        allowed = allowed_figures({"share_percent": 40})
        assert not check("You spent ₹40.", allowed).ok

    def test_a_rupee_figure_does_not_license_a_percentage(self):
        allowed = allowed_figures({"total_rupees": 40})
        assert not check("That is 40% of spending.", allowed).ok

    def test_a_count_does_not_license_a_rupee_figure(self):
        allowed = allowed_figures({"transaction_count": 12})
        assert not check("You spent ₹12.", allowed).ok

    def test_a_rupee_figure_may_be_quoted_as_a_bare_number(self):
        """Bare numbers fall back to every kind, on purpose — "12,458 rupees"
        and "₹12,458" are the same claim."""
        allowed = allowed_figures({"total_rupees": 12458})
        assert check("That comes to 12,458 rupees.", allowed).ok
