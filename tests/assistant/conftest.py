"""Fixtures for the assistant suite.

One month of real transactions through the real pipeline, as the intelligence
suite does. The point of the assistant is to answer questions about a genuine
ledger, and a hand-built row set would let a tool pass while being wrong about
data that actually occurs — an unmatched payee, a NACH mandate, a refund.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from app.ai.base import (
    AIProvider,
    ConversationTurn,
    Message,
    ProviderCost,
    ProviderResponse,
    ToolCall,
)
from app.core.config import settings
from app.db.session import scoped_session
from app.extraction.pipeline import parse_document
from app.services import ledger

from tests.conftest import register_user

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "statements"
FIXTURE = FIXTURES / "hdfc-2024-03.pdf"

pytestmark = pytest.mark.skipif(
    not FIXTURE.exists(), reason="run `make gen-fixtures` first"
)


class FakeProvider(AIProvider):
    """Says whatever the test tells it to, and records what it was shown.

    Registered under the name "gemini" so the gateway's bookkeeping writes the
    same rows it would in production. Nothing here reaches a network.
    """

    name = "gemini"
    pricing = {"gemini-2.0-flash": ProviderCost(Decimal("8.30"), Decimal("33.20"))}

    def __init__(self, *turns: ConversationTurn) -> None:
        self.turns = list(turns)
        self.seen: list[list[Message]] = []
        self.systems: list[str] = []

    def available(self) -> bool:
        return True

    async def classify(self, payload, *, model, categories, timeout_seconds):
        return ProviderResponse(raw=None, model_name=model, error_code="not_used")

    async def converse(
        self, *, system_instruction, messages, declarations, model,
        timeout_seconds, max_output_tokens=800,
    ):
        # A copy: the orchestrator keeps appending to the same list, and a test
        # asserting on what turn 1 was shown must not see turn 3's transcript.
        self.seen.append(list(messages))
        self.systems.append(system_instruction)
        if not self.turns:
            return ConversationTurn(text="No further answer.", model_name=model)
        turn = self.turns.pop(0)
        turn.model_name = model
        return turn


def says(text_value: str) -> ConversationTurn:
    return ConversationTurn(text=text_value, input_tokens=100, output_tokens=30)


def calls(name: str, **arguments) -> ConversationTurn:
    return ConversationTurn(
        tool_calls=(ToolCall(name=name, arguments=arguments),),
        input_tokens=100,
        output_tokens=10,
    )


@pytest.fixture
def ai_on(monkeypatch):
    monkeypatch.setattr(settings, "AI_ENABLED", True)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "test-key-not-real")


@pytest.fixture
async def tenant(client) -> uuid.UUID:
    user = await register_user(client)
    return uuid.UUID(user["user"]["tenant_id"])


@pytest.fixture
async def imported(tenant) -> uuid.UUID:
    """A real statement, parsed and persisted."""
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
