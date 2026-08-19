"""The provider contract.

Every model vendor sits behind this. Only Gemini is implemented; the rest are
registered names with docstrings mapping them to this interface, so adding one
is a single file rather than a refactor.

Note what a provider is *not* allowed to be. It receives an already-validated
:class:`AIPayload` and returns a raw dictionary. It never sees a transaction, a
session, a tenant id or a database handle; it cannot decide what to send,
because by the time it is called that decision is made and enforced. A provider
is a transport, and keeping it that thin is what makes the privacy perimeter
checkable in one place instead of once per vendor.

The assistant path (P8) adds :meth:`AIProvider.converse`, which is the same
principle applied to a multi-turn call. The provider is handed a system
instruction, a transcript and a set of function *declarations*; it returns
either text or a request to call one of them. It does not execute anything.
Tools run here, against a tenant-scoped session, and the provider never learns
that a database exists. Automatic function calling — where the vendor SDK
invokes your Python functions for you — is explicitly disabled: it would move
tool execution inside the transport, which is precisely the boundary this
interface exists to hold.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Any, ClassVar, Literal, Mapping

from app.privacy.allowlist import AIPayload


@dataclass(slots=True)
class ProviderResponse:
    """What came back, before anything believes it."""

    #: The parsed JSON object. Untrusted until `output_validator` says otherwise.
    raw: dict[str, Any] | None
    model_name: str
    model_version: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    #: Set when the call itself failed. A code, never a provider message —
    #: those can echo the prompt.
    error_code: str | None = None

    @property
    def ok(self) -> bool:
        return self.raw is not None and self.error_code is None


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A function the model wants run. It does not get to run it."""

    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Message:
    """One turn of a conversation, in provider-neutral form.

    Deliberately not the vendor's message type. Translating once per provider
    keeps the orchestrator — where the privacy and traceability rules live —
    written against something that cannot quietly acquire a vendor-specific
    field with vendor-specific semantics.
    """

    role: Literal["user", "model", "tool"]
    text: str | None = None
    #: Set when ``role`` is "model" and the model asked for tools.
    tool_calls: tuple[ToolCall, ...] = ()
    #: Set when ``role`` is "tool": which tool, and its redacted result.
    tool_name: str | None = None
    tool_result: dict[str, Any] | None = None


@dataclass(slots=True)
class ConversationTurn:
    """What came back from one round of :meth:`AIProvider.converse`."""

    text: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    model_name: str = ""
    model_version: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    error_code: str | None = None

    @property
    def ok(self) -> bool:
        return self.error_code is None and (
            self.text is not None or bool(self.tool_calls)
        )


@dataclass(slots=True)
class ProviderCost:
    input_per_million_inr: Decimal
    output_per_million_inr: Decimal

    def estimate(self, input_tokens: int, output_tokens: int) -> Decimal:
        million = Decimal("1000000")
        return (
            Decimal(input_tokens) / million * self.input_per_million_inr
            + Decimal(output_tokens) / million * self.output_per_million_inr
        ).quantize(Decimal("0.000001"))


class AIProvider(ABC):
    """A transport to one vendor. Nothing more."""

    #: Registry key, and the value stored on every ai_classifications row.
    name: str = "unknown"

    #: Rupee pricing per million tokens, by model. Configured rather than
    #: fetched: a wrong number here overstates or understates spend, but a
    #: network call to find out would make classification depend on a second
    #: service being up.
    #:
    #: A ClassVar with an immutable default — this is a plain ABC, not a
    #: dataclass, so `field(default_factory=dict)` here is a dataclasses
    #: sentinel rather than a dict, and any subclass that does not override it
    #: fails on the first cost lookup.
    pricing: ClassVar[Mapping[str, ProviderCost]] = MappingProxyType({})

    @abstractmethod
    async def classify(
        self, payload: AIPayload, *, model: str, categories: tuple[str, ...],
        timeout_seconds: int,
    ) -> ProviderResponse:
        """Ask for a category. Returns raw, unvalidated output."""

    @abstractmethod
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
        """One round of a tool-using conversation.

        Returns text, or the tools the model wants called — never both acted
        upon here. ``declarations`` describe functions; the provider must not
        execute them.
        """

    @abstractmethod
    def available(self) -> bool:
        """Whether this provider is configured well enough to be called."""

    def cost_for(self, model: str, input_tokens: int, output_tokens: int) -> Decimal:
        pricing = self.pricing.get(model)
        if pricing is None:
            return Decimal("0.000000")
        return pricing.estimate(input_tokens, output_tokens)


class ProviderUnavailable(RuntimeError):
    """Raised when a named provider exists but is not implemented or configured."""
