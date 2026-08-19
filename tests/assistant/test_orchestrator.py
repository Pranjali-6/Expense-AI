"""The loop around the model, and every way out of it.

The design claim under test is that there is no failure mode worse than plainer
wording. So each test breaks the model path in a different way — an invented
figure, a URL in the answer, a provider error, an endless appetite for tools,
an argument naming another tenant — and asserts the same two things: the user
still gets a correct answer, and the system recorded what happened.

The fake provider makes that testable without a key or a network. It is not a
stand-in for "the model works"; it is a way to script the specific
misbehaviours a real model exhibits occasionally and a suite must exercise
every time.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.ai.base import ConversationTurn, ToolCall
from app.assistant import orchestrator
from app.assistant.orchestrator import Source
from app.core.config import settings
from app.db.session import scoped_session

from tests.assistant.conftest import FakeProvider, calls, says


async def _ask(tenant: uuid.UUID, question: str, provider=None, suggestion_id=None):
    async with scoped_session(tenant) as session:
        return await orchestrator.answer(
            session,
            tenant_id=tenant,
            question=question,
            suggestion_id=suggestion_id,
            provider=provider,
        )


async def _incidents(tenant: uuid.UUID) -> list[str]:
    async with scoped_session(tenant) as session:
        rows = (
            await session.execute(text("SELECT kind FROM privacy_incidents"))
        ).scalars().all()
    return list(rows)


async def _counters(tenant: uuid.UUID) -> dict:
    async with scoped_session(tenant) as session:
        row = (
            await session.execute(
                text(
                    "SELECT COALESCE(SUM(ai_calls_made),0) AS calls, "
                    "COALESCE(SUM(outputs_rejected),0) AS rejected "
                    "FROM privacy_counters"
                )
            )
        ).one()
    return dict(row._mapping)


class TestWithoutAModel:
    """The default configuration, and the one the product is designed around."""

    async def test_every_canned_question_is_answered(self, imported):
        from app.assistant.deterministic import SUGGESTIONS

        for suggestion in SUGGESTIONS:
            answer = await _ask(imported, "", suggestion_id=suggestion.id)
            assert answer.source == Source.DETERMINISTIC, suggestion.id
            assert answer.text and answer.cards, suggestion.id
            assert answer.cards[0].tool == suggestion.tool

    @pytest.mark.parametrize(
        ("question", "expected_tool"),
        [
            ("How much did I spend on food this month?", "get_category_spending"),
            ("What subscriptions do I have?", "get_recurring_expenses"),
            ("Show me transactions above ₹10,000", "get_transactions"),
            ("Why did my spending increase?", "compare_months"),
            ("Where did most of my money go?", "get_top_merchants"),
            ("Is anything unusual?", "get_anomalies"),
            ("How much did I spend last month?", "get_monthly_spending"),
        ],
    )
    async def test_typed_questions_route_to_the_right_tool(
        self, imported, question, expected_tool
    ):
        answer = await _ask(imported, question)
        assert answer.cards and answer.cards[0].tool == expected_tool

    async def test_an_unrecognised_question_refuses_rather_than_guesses(self, imported):
        answer = await _ask(imported, "What is the capital of Assam?")
        assert answer.source == Source.UNAVAILABLE
        assert not answer.cards

    async def test_nothing_is_billed(self, imported):
        await _ask(imported, "How much did I spend this month?")
        assert (await _counters(imported))["calls"] == 0


class TestWithAModel:
    async def test_a_traceable_answer_is_kept(self, imported, ai_on):
        provider = FakeProvider(
            calls("get_monthly_spending", month="2024-03"),
            ConversationTurn(text="", input_tokens=1, output_tokens=1),
        )
        # Phrase the answer from the figures the tool actually returned.
        async with scoped_session(imported) as session:
            from app.assistant import executor

            from datetime import date

            execution = await executor.execute(
                session,
                name="get_monthly_spending",
                arguments={"month": "2024-03"},
                default_month=date(2024, 3, 1),
            )
        spend = execution.result.model_view["spending_rupees"]
        provider.turns[1] = says(f"You spent ₹{spend:,} in March 2024.")

        answer = await _ask(imported, "How much did I spend?", provider=provider)

        assert answer.source == Source.MODEL
        assert str(spend) in answer.text.replace(",", "")
        assert answer.cards and answer.cards[0].tool == "get_monthly_spending"
        assert answer.model_name

    async def test_an_invented_figure_discards_the_wording(self, imported, ai_on):
        """The branch this whole design exists for."""
        provider = FakeProvider(
            calls("get_monthly_spending", month="2024-03"),
            says("You spent ₹9,99,999 in March 2024, which is a lot."),
        )
        answer = await _ask(imported, "How much did I spend?", provider=provider)

        assert answer.source == Source.DETERMINISTIC
        assert "9,99,999" not in answer.text
        # The figures the model legitimately fetched are kept — only the prose
        # was rejected.
        assert answer.cards and answer.cards[0].tool == "get_monthly_spending"
        assert any("discarded" in note for note in answer.notes)
        assert "output_untraceable_figure" in await _incidents(imported)

    async def test_a_url_in_the_answer_is_rejected(self, imported, ai_on):
        provider = FakeProvider(
            calls("get_monthly_spending", month="2024-03"),
            says("See https://example.com/statement for the full breakdown."),
        )
        answer = await _ask(imported, "How much did I spend?", provider=provider)
        assert answer.source == Source.DETERMINISTIC
        assert "example.com" not in answer.text
        assert (await _counters(imported))["rejected"] >= 1

    async def test_a_provider_error_falls_back(self, imported, ai_on):
        provider = FakeProvider(ConversationTurn(error_code="DeadlineExceeded"))
        answer = await _ask(
            imported, "How much did I spend this month?", provider=provider
        )
        assert answer.source == Source.DETERMINISTIC
        assert answer.text

    async def test_the_tool_budget_is_enforced(self, imported, ai_on):
        """A model that keeps asking is stopped, not indulged."""
        provider = FakeProvider(
            *[calls("get_monthly_spending", month="2024-03") for _ in range(8)]
        )
        answer = await _ask(
            imported, "How much did I spend this month?", provider=provider
        )
        assert answer.source == Source.DETERMINISTIC
        assert len(provider.seen) <= orchestrator.MAX_TOOL_CALLS + 1

    async def test_a_bad_tool_call_is_reported_and_the_answer_continues(
        self, imported, ai_on
    ):
        """An identity argument is refused, and the model is told so."""
        provider = FakeProvider(
            ConversationTurn(
                tool_calls=(
                    ToolCall(
                        name="get_monthly_spending",
                        arguments={"tenant_id": str(uuid.uuid4())},
                    ),
                )
            ),
            says("I could not retrieve that."),
        )
        answer = await _ask(imported, "How much did I spend?", provider=provider)

        tool_turn = provider.seen[-1][-1]
        assert tool_turn.role == "tool"
        assert "error" in (tool_turn.tool_result or {})
        assert answer.source == Source.MODEL

    async def test_a_canned_question_never_reaches_the_model(self, imported, ai_on):
        provider = FakeProvider(says("should not be used"))
        answer = await _ask(imported, "", suggestion_id="subscriptions", provider=provider)
        assert provider.seen == []
        assert answer.source == Source.DETERMINISTIC

    async def test_the_model_sees_no_description_and_no_raw_amount(
        self, imported, ai_on
    ):
        provider = FakeProvider(
            calls("get_transactions", period="2024-03", limit=5),
            says("Nothing to report."),
        )
        await _ask(imported, "Show me transactions", provider=provider)

        tool_messages = [
            message
            for transcript in provider.seen
            for message in transcript
            if message.role == "tool"
        ]
        assert tool_messages
        for message in tool_messages:
            for row in (message.tool_result or {}).get("transactions", []):
                assert "description" not in row
                assert isinstance(row["amount_rupees"], int)

    async def test_withholding_a_payee_is_said_out_loud(self, imported, ai_on):
        """The user is told what was held back, not left to notice.

        A card showing "Sneha Kulkarni" beside a sentence saying "an unnamed
        payee" is confusing unless the reason is stated. It is stated.
        """
        provider = FakeProvider(
            calls("get_top_merchants", limit=20),
            says("Nothing to report."),
        )
        answer = await _ask(imported, "Where did my money go?", provider=provider)

        card = next(c for c in answer.cards if c.tool == "get_top_merchants")
        tool_result = next(
            m.tool_result
            for transcript in provider.seen
            for m in transcript
            if m.role == "tool"
        )
        withheld = [
            row for row in (tool_result or {})["merchants"] if row["merchant_withheld"]
        ]
        if not withheld:
            pytest.skip("this fixture has no unmatched non-card payees")

        assert any("not sent to the model" in note for note in answer.notes)
        # And the card still names them, because the user is not the party the
        # name is being withheld from.
        assert any(row["merchant"] for row in card.data["merchants"])

    async def test_the_current_month_is_supplied_rather_than_inferred(
        self, imported, ai_on
    ):
        provider = FakeProvider(says("Nothing to report."))
        await _ask(imported, "How much did I spend this month?", provider=provider)
        assert "2024-03" in provider.systems[0]

    async def test_the_budget_ceiling_stops_the_model_path(
        self, imported, ai_on, monkeypatch
    ):
        from decimal import Decimal

        monkeypatch.setattr(settings, "AI_MONTHLY_BUDGET_INR", Decimal("0"))
        provider = FakeProvider(says("should not be used"))
        answer = await _ask(
            imported, "How much did I spend this month?", provider=provider
        )
        assert provider.seen == []
        assert answer.source == Source.DETERMINISTIC
