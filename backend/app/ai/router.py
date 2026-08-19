"""Which provider handles a call.

A registry rather than an import: the reserved providers are registered by name
so ``/privacy`` can list what exists and what is implemented, and so selecting an
unimplemented one fails with a clear message instead of an ImportError.
"""

from __future__ import annotations

from functools import lru_cache

from app.ai.base import AIProvider, ProviderUnavailable
from app.ai.providers._future import RESERVED
from app.ai.providers.gemini import GeminiProvider
from app.core.config import settings


@lru_cache(maxsize=1)
def _registry() -> dict[str, AIProvider]:
    providers: dict[str, AIProvider] = {"gemini": GeminiProvider()}
    for reserved in RESERVED:
        instance = reserved()
        providers[instance.name] = instance
    return providers


def implemented_providers() -> list[str]:
    return ["gemini"]


def known_providers() -> list[str]:
    return sorted(_registry())


def get_provider(name: str | None = None) -> AIProvider:
    provider = _registry().get(name or settings.AI_PROVIDER)
    if provider is None:
        raise ProviderUnavailable(f"No provider registered under {name!r}.")
    return provider
