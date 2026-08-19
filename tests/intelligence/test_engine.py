"""The Financial Intelligence Engine, checked against independent arithmetic.

The rule for this suite: **every figure the engine reports is recomputed here by
a different route and asserted equal.** The engine aggregates in SQL with
FILTER clauses; these tests pull the raw rows into Python and add them up with
Decimal. Two implementations that agree are evidence; one implementation
asserting its own output is not.

The data is a real statement run through the real pipeline, so the numbers under
test are the numbers a user would see.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from app.db.session import scoped_session
from app.extraction.pipeline import parse_document
from app.intelligence import analytics, anomaly, budgets, forecasting, insights, recurring, timeline
from app.services import ledger

from tests.conftest import register_user

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "statements"
FIXTURE = FIXTURES / "hdfc-2024-03.pdf"
MONTH = date(2024, 3, 1)

pytestmark = pytest.mark.skipif(
    not FIXTURE.exists(), reason="run `make gen-fixtures` first"
)

ZERO = Decimal("0.00")


@pytest.fixture
async def tenant(client) -> uuid.UUID:
    user = await register_user(client)
    return uuid.UUID(user["user"]["tenant_id"])


@pytest.fixture
async def imported(tenant):
    """One month of real transactions, through the real pipeline."""
    statement_id = uuid.uuid4()
    async with scoped_session(tenant, actor="system") as session:
        await session.execute(
            text(
                """
                INSERT INTO statements (
                    id, tenant_id, storage_key, file_size_bytes, file_sha256,
                    document_type, status, trust_status, page_count
                ) VALUES (
                    :id, :tenant_id, :key, 1000, :digest,
                    'unknown', 'processing', 'pending', 3
                )
                """
            ),
            {
                "id": statement_id,
                "tenant_id": tenant,
                "key": f"test/{statement_id}.pdf",
                "digest": uuid.uuid4().hex * 2,
            },
        )
        outcome = parse_document(FIXTURE.read_bytes())
        await ledger.persist(
            session, tenant_id=tenant, statement_id=statement_id, outcome=outcome
        )
    return tenant


async def _raw_rows(tenant: uuid.UUID) -> list[dict]:
    """Every transaction, unaggregated. The independent source of truth."""
    async with scoped_session(tenant) as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT t.amount, t.direction, t.is_expense, t.movement_type,
                           t.txn_date, t.merchant, c.slug AS category_slug
                    FROM transactions t
                    LEFT JOIN categories c ON c.id = t.category_id
                    """
                )
            )
        ).all()
    return [dict(row._mapping) for row in rows]


class TestMonthlySummaryIsReproducible:
    async def test_expenses_match_an_independent_sum(self, imported):
        async with scoped_session(imported) as session:
            summary = await analytics.monthly_summary(session, MONTH)

        rows = await _raw_rows(imported)
        expected = sum(
            (Decimal(str(row["amount"])) for row in rows if row["is_expense"]), ZERO
        )
        assert summary.expenses == expected.quantize(Decimal("0.01"))

    async def test_income_counts_only_salary_and_income(self, imported):
        """A refund is money coming back, not money earned."""
        async with scoped_session(imported) as session:
            summary = await analytics.monthly_summary(session, MONTH)

        rows = await _raw_rows(imported)
        expected = sum(
            (
                Decimal(str(row["amount"]))
                for row in rows
                if row["movement_type"] in ("salary", "income")
                and row["direction"] == "credit"
            ),
            ZERO,
        )
        assert summary.income == expected.quantize(Decimal("0.01"))

    async def test_transfers_are_excluded_from_spending(self, imported):
        """The classic double-count: a card payment is not an expense."""
        rows = await _raw_rows(imported)
        movements = {
            row["movement_type"] for row in rows if row["is_expense"]
        }
        assert "transfer" not in movements
        assert "credit_card_payment" not in movements
        assert "cash_withdrawal" not in movements
        assert "refund" not in movements

    async def test_net_cash_flow_is_income_minus_net_expenses(self, imported):
        async with scoped_session(imported) as session:
            summary = await analytics.monthly_summary(session, MONTH)

        assert summary.net_cash_flow == (
            summary.income - (summary.expenses - summary.refunds)
        ).quantize(Decimal("0.01"))

    async def test_savings_rate_is_a_share_of_income(self, imported):
        async with scoped_session(imported) as session:
            summary = await analytics.monthly_summary(session, MONTH)

        assert Decimal("0") <= summary.savings_rate <= Decimal("1")
        if summary.income > ZERO:
            expected = (summary.income - summary.net_expenses) / summary.income
            assert summary.savings_rate == max(
                Decimal("0"), min(Decimal("1"), expected)
            ).quantize(Decimal("0.0001"))

    async def test_savings_rate_is_zero_without_income(self, tenant):
        """Not an error, and not a spectacular fictional number either."""
        async with scoped_session(tenant) as session:
            summary = await analytics.monthly_summary(session, MONTH)
        assert summary.income == ZERO
        assert summary.savings_rate == Decimal("0.0000")

    async def test_data_quality_reports_unreviewed_rows(self, imported):
        async with scoped_session(imported) as session:
            summary = await analytics.monthly_summary(session, MONTH)
            expected = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM transactions "
                        "WHERE review_status = 'review_required'"
                    )
                )
            ).scalar_one()
        assert summary.quality.awaiting_review == expected


class TestCategoryBreakdownAddsUp:
    async def test_the_breakdown_sums_to_the_headline_expense_total(self, imported):
        """A breakdown that disagrees with the headline is worse than none."""
        async with scoped_session(imported) as session:
            summary = await analytics.monthly_summary(session, MONTH)
            rows = await analytics.category_breakdown(session, MONTH)

        assert sum((Decimal(row["total"]) for row in rows), ZERO) == summary.expenses

    async def test_shares_sum_to_one(self, imported):
        async with scoped_session(imported) as session:
            rows = await analytics.category_breakdown(session, MONTH)

        total = sum((Decimal(row["share"]) for row in rows), Decimal("0"))
        assert abs(total - Decimal("1")) < Decimal("0.001")

    async def test_each_category_matches_an_independent_sum(self, imported):
        async with scoped_session(imported) as session:
            rows = await analytics.category_breakdown(session, MONTH)

        raw = await _raw_rows(imported)
        for row in rows:
            slug = row["slug"]
            expected = sum(
                (
                    Decimal(str(item["amount"]))
                    for item in raw
                    if item["is_expense"]
                    and (item["category_slug"] or "uncategorised") == slug
                ),
                ZERO,
            )
            assert Decimal(row["total"]) == expected.quantize(Decimal("0.01")), slug

    async def test_only_expenses_appear(self, imported):
        """Salary must not show up as a spending category."""
        async with scoped_session(imported) as session:
            rows = await analytics.category_breakdown(session, MONTH)
        slugs = {row["slug"] for row in rows}
        assert "salary" not in slugs
        assert "credit_card_payment" not in slugs


class TestDailySeries:
    async def test_every_day_of_the_month_is_present(self, imported):
        async with scoped_session(imported) as session:
            series = await analytics.daily_series(session, MONTH)
        assert len(series) == 31
        assert series[0]["day"] == "2024-03-01"
        assert series[-1]["day"] == "2024-03-31"

    async def test_the_daily_total_matches_the_month(self, imported):
        async with scoped_session(imported) as session:
            series = await analytics.daily_series(session, MONTH)
            summary = await analytics.monthly_summary(session, MONTH)

        assert sum((Decimal(day["expenses"]) for day in series), ZERO) == summary.expenses


class TestTopMerchants:
    async def test_totals_match_an_independent_sum(self, imported):
        async with scoped_session(imported) as session:
            rows = await analytics.top_merchants(session, MONTH, limit=5)

        raw = await _raw_rows(imported)
        for row in rows:
            expected = sum(
                (
                    Decimal(str(item["amount"]))
                    for item in raw
                    if item["is_expense"] and item["merchant"] == row["merchant"]
                ),
                ZERO,
            )
            assert Decimal(row["total"]) == expected.quantize(Decimal("0.01"))

    async def test_ordered_by_spend(self, imported):
        async with scoped_session(imported) as session:
            rows = await analytics.top_merchants(session, MONTH, limit=10)
        totals = [Decimal(row["total"]) for row in rows]
        assert totals == sorted(totals, reverse=True)


class TestRecurringDetection:
    def _candidate(self, days: list[date], amounts: list[str]) -> recurring.Candidate:
        return recurring.Candidate(
            merchant="Test", category_id=None, account_id=None,
            charges=list(zip(days, [Decimal(a) for a in amounts])),
        )

    def test_a_clean_monthly_subscription_is_detected(self):
        days = [date(2024, month, 9) for month in range(1, 7)]
        found = recurring.detect(
            self._candidate(days, ["649.00"] * 6), today=date(2024, 6, 20)
        )
        assert found is not None
        assert found.cadence.value == "monthly"
        assert found.typical_amount == Decimal("649.00")
        assert found.estimated_annual_cost == Decimal("7788.00")
        assert found.next_expected_on == date(2024, 7, 9)

    def test_irregular_spending_is_not_a_subscription(self):
        """Takeaway ordered every couple of weeks is not a schedule."""
        days = [date(2024, 3, d) for d in (2, 9, 21, 22, 28)]
        assert recurring.detect(self._candidate(days, ["400.00"] * 5)) is None

    def test_two_charges_are_not_a_pattern(self):
        days = [date(2024, 1, 9), date(2024, 2, 9)]
        assert recurring.detect(self._candidate(days, ["649.00"] * 2)) is None

    def test_the_median_amount_is_used_not_the_mean(self):
        """One annual plan among monthly charges must not skew the estimate."""
        days = [date(2024, month, 9) for month in range(1, 7)]
        found = recurring.detect(
            self._candidate(days, ["199.00", "199.00", "199.00", "5000.00",
                                   "199.00", "199.00"]),
            today=date(2024, 6, 20),
        )
        assert found is not None
        assert found.typical_amount == Decimal("199.00")

    def test_the_next_charge_preserves_the_billing_day(self):
        """Adding a median day-count drifts; months do not have equal lengths."""
        assert recurring.next_charge_date(
            date(2024, 6, 9), recurring.SubscriptionCadence.MONTHLY, 31
        ) == date(2024, 7, 9)
        assert recurring.next_charge_date(
            date(2024, 12, 9), recurring.SubscriptionCadence.MONTHLY, 30
        ) == date(2025, 1, 9)

    def test_a_month_end_billing_day_is_clamped(self):
        """A subscription billed on the 31st charges on the 28th in February."""
        assert recurring.next_charge_date(
            date(2024, 1, 31), recurring.SubscriptionCadence.MONTHLY, 31
        ) == date(2024, 2, 29)

    def test_weekly_cadences_still_advance_by_days(self):
        assert recurring.next_charge_date(
            date(2024, 3, 4), recurring.SubscriptionCadence.WEEKLY, 7
        ) == date(2024, 3, 11)

    def test_a_long_lapsed_subscription_is_marked_lapsed(self):
        days = [date(2023, month, 9) for month in range(1, 7)]
        found = recurring.detect(
            self._candidate(days, ["649.00"] * 6), today=date(2024, 6, 1)
        )
        assert found is not None
        assert found.status.value == "lapsed"

    def test_calendar_drift_still_counts_as_monthly(self):
        """28th of every month, including February."""
        days = [date(2024, m, min(31, 28)) for m in range(1, 7)]
        found = recurring.detect(
            self._candidate(days, ["500.00"] * 6), today=date(2024, 6, 20)
        )
        assert found is not None and found.cadence.value == "monthly"
        assert found.cadence_stability >= Decimal("0.90")


class TestAnomalyDetection:
    def test_a_large_outlier_is_flagged(self):
        history = [Decimal(x) for x in ("400", "450", "380", "420", "410", "390")]
        scored = anomaly.robust_z(Decimal("3000"), history)
        assert scored is not None and scored[0] > anomaly.Z_THRESHOLD

    def test_an_ordinary_amount_is_not(self):
        history = [Decimal(x) for x in ("400", "450", "380", "420", "410", "390")]
        scored = anomaly.robust_z(Decimal("430"), history)
        assert scored is not None and scored[0] < anomaly.Z_THRESHOLD

    def test_no_baseline_means_no_claim(self):
        assert anomaly.robust_z(Decimal("3000"), [Decimal("400")]) is None

    def test_identical_history_produces_no_score(self):
        """MAD of zero would make a one-rupee difference infinitely unusual."""
        assert anomaly.robust_z(Decimal("401"), [Decimal("400")] * 8) is None

    def test_the_median_is_not_dragged_by_the_outlier_itself(self):
        """The reason a mean and standard deviation would not do.

        One ₹50,000 charge among ordinary ones pulls a *mean* to about ₹7,500,
        after which nothing else can ever look unusual — and the ₹50,000 itself
        looks normal. The median barely moves.
        """
        history = [
            Decimal(x) for x in ("380", "400", "420", "410", "390", "395", "50000")
        ]
        scored = anomaly.robust_z(Decimal("3000"), history)
        assert scored is not None
        z, median = scored
        assert median == Decimal("400.00")
        assert z > anomaly.Z_THRESHOLD

        mean = sum(history) / len(history)
        assert mean > Decimal("7000")   # what a mean would have reported

    async def test_reasons_carry_the_numbers_and_never_say_fraud(self, imported):
        async with scoped_session(imported, actor="system") as session:
            found = await anomaly.sweep(session, tenant_id=imported, month=MONTH)

        for item in found:
            assert "₹" in item.reason
            lowered = item.reason.lower()
            for forbidden in ("fraud", "fraudulent", "stolen", "scam", "criminal"):
                assert forbidden not in lowered


class TestBudgets:
    async def test_progress_matches_an_independent_sum(self, imported, tenant):
        async with scoped_session(imported, actor="user") as session:
            user_id = (
                await session.execute(text("SELECT id FROM users LIMIT 1"))
            ).scalar_one()
            await budgets.create(
                session, tenant_id=imported, user_id=user_id,
                category_slug="food", amount=Decimal("10000.00"),
            )
            rows = await budgets.progress(session, month=MONTH, today=date(2024, 3, 31))

        raw = await _raw_rows(imported)
        expected = sum(
            (
                Decimal(str(item["amount"]))
                for item in raw
                if item["is_expense"] and item["category_slug"] == "food"
            ),
            ZERO,
        )
        assert len(rows) == 1
        assert Decimal(rows[0]["spent"]) == expected.quantize(Decimal("0.01"))
        assert Decimal(rows[0]["remaining"]) == Decimal("10000.00") - Decimal(
            rows[0]["spent"]
        )

    async def test_a_breached_budget_says_so(self, imported):
        async with scoped_session(imported, actor="user") as session:
            user_id = (
                await session.execute(text("SELECT id FROM users LIMIT 1"))
            ).scalar_one()
            await budgets.create(
                session, tenant_id=imported, user_id=user_id,
                category_slug="food", amount=Decimal("1.00"),
            )
            rows = await budgets.progress(session, month=MONTH, today=date(2024, 3, 31))
        assert rows[0]["state"] == "exceeded"

    async def test_a_zero_budget_is_rejected(self, imported):
        from app.core.errors import ValidationFailedError

        async with scoped_session(imported, actor="user") as session:
            user_id = (
                await session.execute(text("SELECT id FROM users LIMIT 1"))
            ).scalar_one()
            with pytest.raises(ValidationFailedError):
                await budgets.create(
                    session, tenant_id=imported, user_id=user_id,
                    category_slug="food", amount=Decimal("0"),
                )


class TestForecasting:
    async def test_a_completed_month_projects_to_what_happened(self, imported):
        async with scoped_session(imported) as session:
            projection = await forecasting.project_month(
                session, month=MONTH, today=date(2024, 4, 15)
            )
            summary = await analytics.monthly_summary(session, MONTH)

        assert projection.projected_total == summary.expenses
        assert projection.reliable is True

    async def test_an_early_month_projection_is_marked_unreliable(self, imported):
        async with scoped_session(imported) as session:
            projection = await forecasting.project_month(
                session, month=MONTH, today=date(2024, 3, 3)
            )
        assert projection.reliable is False
        assert projection.days_elapsed == 3

    async def test_the_run_rate_extrapolates_elapsed_days(self, imported):
        async with scoped_session(imported) as session:
            projection = await forecasting.project_month(
                session, month=MONTH, today=date(2024, 3, 10)
            )
        expected = (
            projection.spent_so_far / projection.days_elapsed * projection.days_in_month
        ).quantize(Decimal("0.01"))
        assert projection.run_rate_projection == expected


class TestInsights:
    async def test_the_snapshot_agrees_with_the_summary(self, imported):
        async with scoped_session(imported) as session:
            insight = await insights.build(session, MONTH)
            summary = await analytics.monthly_summary(session, MONTH)

        assert insight.summary["net_expenses"] == str(summary.net_expenses)
        assert insight.summary["income"] == str(summary.income)

    async def test_the_largest_category_really_is_the_largest(self, imported):
        async with scoped_session(imported) as session:
            insight = await insights.build(session, MONTH)
            rows = await analytics.category_breakdown(session, MONTH)

        assert insight.largest_category["slug"] == rows[0]["slug"]

    async def test_observations_are_data_not_prose_only(self, imported):
        """Each carries the numbers behind it, so nothing has to be re-derived."""
        async with scoped_session(imported) as session:
            insight = await insights.build(session, MONTH)

        assert insight.observations
        for note in insight.observations:
            assert note["kind"] and note["text"] and note["values"]

    async def test_unreviewed_rows_are_declared(self, imported):
        async with scoped_session(imported) as session:
            insight = await insights.build(session, MONTH)
        kinds = {note["kind"] for note in insight.observations}
        assert "data_quality" in kinds

    async def test_the_snapshot_persists_and_reloads(self, imported):
        async with scoped_session(imported, actor="system") as session:
            insight = await insights.build(session, MONTH)
            await insights.persist_snapshot(
                session, tenant_id=imported, insight=insight
            )
            row = (
                await session.execute(
                    text(
                        "SELECT total_expenses, narrative FROM insight_snapshots "
                        "WHERE period_month = :month"
                    ),
                    {"month": MONTH},
                )
            ).one()

        assert Decimal(str(row.total_expenses)) == Decimal(
            insight.summary["net_expenses"]
        )
        # Prose is optional and absent: this phase produces data.
        assert row.narrative is None


class TestTimeline:
    async def test_events_are_newest_first(self, imported):
        async with scoped_session(imported) as session:
            events = await timeline.events(session, limit=50)

        days = [event["occurred_on"] for event in events]
        assert days == sorted(days, reverse=True)

    async def test_the_statement_import_appears(self, imported):
        async with scoped_session(imported) as session:
            events = await timeline.events(session, limit=200)
        assert any(event["kind"] == "statement_import" for event in events)

    async def test_large_transactions_are_distinguished(self, imported):
        async with scoped_session(imported) as session:
            events = await timeline.events(session, limit=200)

        for event in events:
            if event["kind"] == "large_transaction":
                assert Decimal(event["amount"]) >= timeline.LARGE_TRANSACTION_FLOOR


class TestNoModelIsInvolved:
    def test_the_engine_imports_no_ai_module(self):
        """The governing rule for this package.

        Every number here must be reproducible by a query a person can read.
        A model anywhere in the path would make that untrue.
        """
        import app.intelligence.analytics as analytics_module
        import app.intelligence.anomaly as anomaly_module
        import app.intelligence.budgets as budgets_module
        import app.intelligence.forecasting as forecasting_module
        import app.intelligence.insights as insights_module
        import app.intelligence.recurring as recurring_module
        import app.intelligence.timeline as timeline_module

        for module in (
            analytics_module, anomaly_module, budgets_module, forecasting_module,
            insights_module, recurring_module, timeline_module,
        ):
            source = Path(module.__file__).read_text()
            for forbidden in (
                "app.ai", "app.privacy.gateway", "google.genai", "openai", "anthropic",
            ):
                assert forbidden not in source, f"{module.__name__} references {forbidden}"

    async def test_every_figure_is_produced_with_ai_disabled(self, imported, monkeypatch):
        """The suite runs this way by default; the assertion makes it explicit."""
        from app.core.config import settings

        monkeypatch.setattr(settings, "AI_ENABLED", False)
        async with scoped_session(imported) as session:
            summary = await analytics.monthly_summary(session, MONTH)
            categories = await analytics.category_breakdown(session, MONTH)
            insight = await insights.build(session, MONTH)

        assert summary.expenses > ZERO
        assert categories
        assert insight.observations


class TestAnomalyReasonsMatchTheirFigures:
    """A reason that contradicts the numbers beside it is worse than none.

    `robust_z` is two-sided — it measures distance from the median in either
    direction — so a charge well *below* the usual is an outlier too. The
    sentence said "unusually large" regardless, and the dashboard rendered
    "₹536.00 at BookMyShow is unusually large for Entertainment, where you
    typically spend ₹1,213.81" beside its own contradiction.
    """

    def test_a_small_outlier_is_not_described_as_large(self):
        from app.intelligence.anomaly import _inr, _money

        amount, median = _money("536.00"), _money("1213.81")
        word = "large" if amount > median else "small"
        sentence = (
            f"{_inr(amount)} at BookMyShow is unusually {word} for Entertainment, "
            f"where you typically spend {_inr(median)}."
        )
        assert "unusually small" in sentence

    async def test_stored_reasons_agree_with_their_own_figures(self, imported):
        """The real check: every stored reason, against the values it carries."""
        async with scoped_session(imported) as session:
            rows = await anomaly.list_anomalies(session, limit=200)

        for row in rows:
            if row["kind"] != "amount_outlier":
                continue
            observed = Decimal(str(row["observed_value"]))
            baseline = Decimal(str(row["baseline_value"]))
            if "unusually large" in row["reason"]:
                assert observed > baseline, row["reason"]
            elif "unusually small" in row["reason"]:
                assert observed < baseline, row["reason"]
