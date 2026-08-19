"""Google Gemini — the one implemented provider.

Structured output is requested through ``response_schema`` with the category
field constrained to an ``enum``, so an off-list answer is refused by the API
rather than caught downstream. The output validator still checks it: the schema
is the vendor's promise, and the validator is ours.

Temperature is pinned to 0. Categorisation is not a creative task, and a
non-deterministic answer to the same transaction would make the explanation
shown to the user ("AI suggested Food") unreproducible. The same reasoning
holds for the assistant: two different phrasings of the same figures is not a
feature when the figures are someone's rent.

``converse`` disables the SDK's automatic function calling. Left on, the client
would invoke Python callables itself, inside the transport, with no chance for
the orchestrator to validate arguments, scope the session or redact the result.
Tool execution belongs on our side of the boundary, so the SDK is told to hand
the request back instead.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from typing import Any

from app.ai.base import (
    AIProvider,
    ConversationTurn,
    Message,
    ProviderCost,
    ProviderResponse,
    ToolCall,
)
from app.ai.prompts import SYSTEM_INSTRUCTION, build_prompt, response_schema
from app.core.config import settings
from app.core.logging import get_logger
from app.privacy.allowlist import AIPayload

logger = get_logger(__name__)


class GeminiProvider(AIProvider):
    name = "gemini"

    # Rupees per million tokens. Approximate and configurable; the purpose is a
    # spend figure the Privacy Center can show, not billing-grade accounting.
    pricing = {
        "gemini-2.0-flash": ProviderCost(Decimal("8.30"), Decimal("33.20")),
        "gemini-2.0-flash-lite": ProviderCost(Decimal("6.20"), Decimal("24.90")),
        "gemini-1.5-flash": ProviderCost(Decimal("6.20"), Decimal("24.90")),
    }

    def __init__(self) -> None:
        self._client: Any = None

    def available(self) -> bool:
        return bool(settings.GEMINI_API_KEY)

    def _get_client(self) -> Any:
        if self._client is None:
            from google import genai

            self._client = genai.Client(api_key=settings.GEMINI_API_KEY)
        return self._client

    async def classify(
        self,
        payload: AIPayload,
        *,
        model: str,
        categories: tuple[str, ...],
        timeout_seconds: int,
    ) -> ProviderResponse:
        from google.genai import types

        started = time.perf_counter()
        prompt = build_prompt(payload, categories)

        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            temperature=0.0,
            response_mime_type="application/json",
            response_schema=response_schema(categories),
            max_output_tokens=200,
        )

        try:
            response = await self._get_client().aio.models.generate_content(
                model=model, contents=prompt, config=config
            )
        except Exception as exc:
            # The provider's message can quote the prompt back. Only the
            # exception *type* is recorded.
            logger.warning(
                "ai_provider_call_failed",
                model_name=model,
                error_code=type(exc).__name__,
            )
            return ProviderResponse(
                raw=None, model_name=model, error_code=type(exc).__name__,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )

        latency_ms = int((time.perf_counter() - started) * 1000)
        usage = getattr(response, "usage_metadata", None)

        raw: dict[str, Any] | None
        try:
            text = response.text or ""
            parsed = json.loads(text)
            raw = parsed if isinstance(parsed, dict) else None
        except (json.JSONDecodeError, AttributeError, TypeError):
            raw = None

        return ProviderResponse(
            raw=raw,
            model_name=model,
            model_version=getattr(response, "model_version", None),
            input_tokens=int(getattr(usage, "prompt_token_count", 0) or 0),
            output_tokens=int(getattr(usage, "candidates_token_count", 0) or 0),
            latency_ms=latency_ms,
            error_code=None if raw is not None else "unparseable_response",
        )

    # ----------------------------------------------------------------------- #
    # Tool-using conversation (the assistant path)
    # ----------------------------------------------------------------------- #

    @staticmethod
    def _to_contents(messages: list[Message]) -> list:
        """Translate the neutral transcript into Gemini's ``Content`` list."""
        from google.genai import types

        contents = []
        for message in messages:
            if message.role == "tool":
                contents.append(
                    types.Content(
                        role="user",
                        parts=[
                            types.Part.from_function_response(
                                name=message.tool_name or "unknown",
                                response=message.tool_result or {},
                            )
                        ],
                    )
                )
            elif message.role == "model" and message.tool_calls:
                contents.append(
                    types.Content(
                        role="model",
                        parts=[
                            types.Part(
                                function_call=types.FunctionCall(
                                    name=call.name, args=call.arguments
                                )
                            )
                            for call in message.tool_calls
                        ],
                    )
                )
            else:
                contents.append(
                    types.Content(
                        role="model" if message.role == "model" else "user",
                        parts=[types.Part(text=message.text or "")],
                    )
                )
        return contents

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
        from google.genai import types

        started = time.perf_counter()

        tools = (
            [
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(**declaration)
                        for declaration in declarations
                    ]
                )
            ]
            if declarations
            else None
        )

        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=0.0,
            max_output_tokens=max_output_tokens,
            tools=tools,
            # See the module docstring: tool execution stays on our side.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True
            ),
        )

        try:
            response = await self._get_client().aio.models.generate_content(
                model=model,
                contents=self._to_contents(messages),
                config=config,
            )
        except Exception as exc:
            # Type only. A provider error message can quote the prompt back,
            # and the prompt contains the user's figures.
            logger.warning(
                "ai_converse_failed", model_name=model, error_code=type(exc).__name__
            )
            return ConversationTurn(
                model_name=model,
                error_code=type(exc).__name__,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )

        latency_ms = int((time.perf_counter() - started) * 1000)
        usage = getattr(response, "usage_metadata", None)

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        candidates = getattr(response, "candidates", None) or []
        for candidate in candidates:
            content = getattr(candidate, "content", None)
            for part in getattr(content, "parts", None) or []:
                call = getattr(part, "function_call", None)
                if call is not None and getattr(call, "name", None):
                    calls.append(
                        ToolCall(name=call.name, arguments=dict(call.args or {}))
                    )
                elif getattr(part, "text", None):
                    text_parts.append(part.text)

        text = "".join(text_parts).strip() or None

        return ConversationTurn(
            text=text,
            tool_calls=tuple(calls),
            model_name=model,
            model_version=getattr(response, "model_version", None),
            input_tokens=int(getattr(usage, "prompt_token_count", 0) or 0),
            output_tokens=int(getattr(usage, "candidates_token_count", 0) or 0),
            latency_ms=latency_ms,
            error_code=None if (text or calls) else "empty_response",
        )
