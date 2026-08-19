"""The reserved interfaces, tested as interfaces rather than as promises.

Two abstractions in this codebase have exactly one implementation and several
reserved names: AI providers and ingestion sources. The value of a reserved
name is that adding the real thing is a single file — and the only way to know
that is true is to check that the abstraction is shaped like the general case
rather than like the one implementation with a class drawn around it.

So: every reserved class must actually implement its ABC (not merely be
mentioned in a docstring), must be constructible, must report itself
unavailable, and must fail loudly and specifically when used.
"""

from __future__ import annotations

import uuid

import pytest

from app.ai.base import AIProvider
from app.ai.providers._future import RESERVED as RESERVED_PROVIDERS
from app.ai.router import implemented_providers, known_providers
from app.ingestion import (
    IngestionSource,
    get_source,
    implemented_sources,
    known_sources,
)
from app.ingestion.sources._future import RESERVED as RESERVED_SOURCES


class TestReservedAIProviders:
    def test_only_gemini_is_implemented(self):
        assert implemented_providers() == ["gemini"]
        assert set(known_providers()) == {
            "gemini", "openai", "azure_openai", "anthropic", "azure_ai_foundry",
        }

    @pytest.mark.parametrize("provider", RESERVED_PROVIDERS, ids=lambda p: p.name)
    def test_each_reserved_provider_really_implements_the_abc(self, provider):
        instance = provider()
        assert isinstance(instance, AIProvider)
        assert instance.available() is False

    @pytest.mark.parametrize("provider", RESERVED_PROVIDERS, ids=lambda p: p.name)
    async def test_a_reserved_provider_fails_with_an_actionable_message(self, provider):
        instance = provider()
        with pytest.raises(NotImplementedError) as raised:
            await instance.classify(
                None, model="x", categories=("other",), timeout_seconds=1
            )
        assert "app.ai.router" in str(raised.value)

    @pytest.mark.parametrize("provider", RESERVED_PROVIDERS, ids=lambda p: p.name)
    async def test_reserved_providers_cover_the_whole_contract(self, provider):
        """`converse` was added for the assistant. A reserved provider that
        implemented only half the interface would fail at the first assistant
        question rather than at construction."""
        instance = provider()
        with pytest.raises(NotImplementedError):
            await instance.converse(
                system_instruction="", messages=[], declarations=[],
                model="x", timeout_seconds=1,
            )


class TestReservedIngestionSources:
    def test_only_pdf_upload_is_implemented(self):
        assert implemented_sources() == ["pdf_upload"]

    def test_the_registry_matches_the_schema_enum(self):
        """`ingestion_sources.source_type` has a CHECK constraint listing four
        values. A registry that drifted from it would let the application offer
        a source the database refuses to record."""
        assert set(known_sources()) == {
            "pdf_upload", "csv", "api", "account_aggregator"
        }

    @pytest.mark.parametrize("source", RESERVED_SOURCES, ids=lambda s: s.source_type)
    def test_each_reserved_source_really_implements_the_abc(self, source):
        instance = source()
        assert isinstance(instance, IngestionSource)
        assert instance.available() is False

    @pytest.mark.parametrize("source", RESERVED_SOURCES, ids=lambda s: s.source_type)
    async def test_a_reserved_source_fails_with_an_actionable_message(self, source):
        instance = source()
        with pytest.raises(NotImplementedError) as raised:
            async for _ in instance.fetch(tenant_id=uuid.uuid4()):
                pass
        assert "app.ingestion.get_source" in str(raised.value)

    async def test_the_implemented_source_yields_what_it_was_given(self):
        source = get_source("pdf_upload")
        assert source.available() is True

        source._uploads = [("march.pdf", b"%PDF-1.4 test")]
        documents = [
            document async for document in source.fetch(tenant_id=uuid.uuid4())
        ]

        assert len(documents) == 1
        assert documents[0].filename == "march.pdf"
        assert documents[0].content == b"%PDF-1.4 test"
        assert documents[0].fetched_at is not None

    def test_an_unknown_source_is_refused(self):
        from app.ingestion import SourceUnavailable

        with pytest.raises(SourceUnavailable):
            get_source("carrier_pigeon")

    def test_only_the_unattended_sources_claim_polling(self):
        """A browser upload has no history to poll; an aggregator does. If that
        flag were wrong, a scheduler would either miss data or poll a source
        that cannot answer."""
        assert get_source("pdf_upload").supports_polling is False
        assert get_source("account_aggregator").supports_polling is True
