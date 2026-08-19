"""Providers that are reserved, not implemented.

Each is a real class implementing :class:`~app.ai.base.AIProvider` and raising
``NotImplementedError`` from ``classify``. They exist so the abstraction is
demonstrably provider-shaped rather than Gemini-shaped with an interface drawn
around it afterwards — the difference shows up the day a second provider is
added, and it is the difference between one file and a refactor.

Adding one means implementing ``classify``, ``converse`` and ``available`` and
registering it in ``app.ai.router``. Nothing else changes: the privacy gateway, the payload
model and the output validator are provider-independent by construction.

The Anthropic provider, when built, will be written against the ``claude-api``
skill rather than from memory.
"""

from __future__ import annotations

from typing import Any

from app.ai.base import AIProvider, ConversationTurn, Message, ProviderResponse
from app.privacy.allowlist import AIPayload


class _ReservedProvider(AIProvider):
    """Shared body for the reserved providers."""

    docs_url = ""

    def available(self) -> bool:
        return False

    async def classify(
        self, payload: AIPayload, *, model: str, categories: tuple[str, ...],
        timeout_seconds: int,
    ) -> ProviderResponse:
        raise NotImplementedError(
            f"The {self.name} provider is reserved but not implemented. "
            "Implement classify() and register it in app.ai.router."
        )

    async def converse(
        self,
        *,
        system_instruction: str,
        messages: list[Message],
        declarations: list[dict[str, Any]],
        model: str,
        timeout_seconds: int,
        max_output_tokens: int = 800,
    ) -> ConversationTurn:
        raise NotImplementedError(
            f"The {self.name} provider is reserved but not implemented. "
            "Implement converse() and register it in app.ai.router."
        )


class OpenAIProvider(_ReservedProvider):
    """Maps to Chat Completions with `response_format={'type': 'json_schema'}`."""

    name = "openai"


class AzureOpenAIProvider(_ReservedProvider):
    """As OpenAI, with a deployment name in place of a model name."""

    name = "azure_openai"


class AnthropicProvider(_ReservedProvider):
    """Maps to the Messages API with a tool-shaped structured output."""

    name = "anthropic"


class AzureAIFoundryProvider(_ReservedProvider):
    """Maps to the Foundry inference endpoint."""

    name = "azure_ai_foundry"


RESERVED = (
    OpenAIProvider,
    AzureOpenAIProvider,
    AnthropicProvider,
    AzureAIFoundryProvider,
)
