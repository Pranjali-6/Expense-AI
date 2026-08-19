"""The monthly narrative: written from the snapshot, or not written at all.

"From snapshots only" is the property worth testing, because the tempting
shortcut — hand the model the month's transactions and ask for a summary —
produces better prose and a product where the paragraph and the chart above it
can disagree. These tests assert the model never sees a transaction, that a
paragraph quoting a figure the snapshot does not contain is thrown away, and
that with AI off the column stays null and every screen still works.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import text

from app.assistant import narrative
from app.db.session import scoped_session
from app.intelligence import insights

from tests.assistant.conftest import FakeProvider, says

MONTH = date(2024, 3, 1)


async def _snapshot(tenant: uuid.UUID) -> None:
    async with scoped_session(tenant, actor="system") as session:
        insight = await insights.build(session, MONTH)
        await insights.persist_snapshot(session, tenant_id=tenant, insight=insight)


async def _stored(tenant: uuid.UUID):
    async with scoped_session(tenant) as session:
        return await narrative.stored(session, MONTH)


async def _generate(tenant: uuid.UUID, provider):
    async with scoped_session(tenant, actor="system") as session:
        return await narrative.generate(
            session, tenant_id=tenant, month=MONTH, provider=provider
        )


class TestWithAIDisabled:
    async def test_nothing_is_written_and_nothing_fails(self, imported):
        await _snapshot(imported)
        assert await _generate(imported, FakeProvider(says("x"))) is None
        assert await _stored(imported) is None

    async def test_the_report_still_has_its_observations(self, imported):
        """A null narrative is the designed state, not a degraded one."""
        await _snapshot(imported)
        async with scoped_session(imported) as session:
            report = await insights.build(session, MONTH)
        assert report.observations


class TestWithAIEnabled:
    async def test_a_traceable_paragraph_is_stored(self, imported, ai_on):
        await _snapshot(imported)

        async with scoped_session(imported) as session:
            prepared = await narrative._snapshot_view(session, MONTH)
        view, _ = prepared
        spend = view["spending_rupees"]

        provider = FakeProvider(says(f"You spent ₹{spend:,} in {view['month_label']}."))
        written = await _generate(imported, provider)

        assert written and str(spend) in written.replace(",", "")
        stored = await _stored(imported)
        assert stored and stored["text"] == written
        assert stored["model_name"] and stored["generated_at"]

    async def test_an_invented_figure_is_not_stored(self, imported, ai_on):
        await _snapshot(imported)
        provider = FakeProvider(says("You spent ₹8,88,888 in March 2024."))

        assert await _generate(imported, provider) is None
        assert await _stored(imported) is None

        async with scoped_session(imported) as session:
            kinds = (
                await session.execute(text("SELECT kind FROM privacy_incidents"))
            ).scalars().all()
        assert "output_untraceable_figure" in kinds

    async def test_the_model_is_given_the_snapshot_and_no_tools(self, imported, ai_on):
        await _snapshot(imported)
        provider = FakeProvider(says("Nothing notable."))
        await _generate(imported, provider)

        # A narrative has nothing to look up: everything it may say is in what
        # it was handed. Offering tools would be offering a way to reach the
        # ledger the snapshot was built to stand in for.
        assert provider.seen
        sent = provider.seen[0][0].text or ""
        assert "spending_rupees" in sent
        assert "description" not in sent

    async def test_a_tool_call_from_a_toolless_call_is_refused(self, imported, ai_on):
        from app.ai.base import ConversationTurn, ToolCall

        await _snapshot(imported)
        provider = FakeProvider(
            ConversationTurn(tool_calls=(ToolCall(name="get_transactions", arguments={}),))
        )
        assert await _generate(imported, provider) is None

    async def test_a_missing_snapshot_writes_nothing(self, imported, ai_on):
        """No snapshot means no source, and no source means no paragraph."""
        provider = FakeProvider(says("Anything at all."))
        async with scoped_session(imported, actor="system") as session:
            written = await narrative.generate(
                session, tenant_id=imported, month=date(2019, 1, 1), provider=provider
            )
        assert written is None
        assert provider.seen == []
