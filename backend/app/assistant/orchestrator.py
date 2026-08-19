"""Answering a question, and refusing to guess.

The shape of this module is the argument for the whole design: the model is one
step in the middle, and every step around it is deterministic.

    question → plan (code, or the model) → tools (code) → figures (code)
             → phrasing (the model) → traceability check (code) → answer

The model may choose which tools to call and how to word the result. It may not
decide who is asking, what the numbers are, or whether its own answer is sound.
Each of those is enforced somewhere it cannot reach:

* **Who is asking** — the session is scoped from the access token before this
  module is entered. There is no tenant argument anywhere in the tool schemas.
* **What the numbers are** — computed by the Intelligence Engine, redacted, and
  handed over finished.
* **Whether the answer is sound** — checked afterwards against those figures,
  and discarded if it does not hold up.

Three budgets bound the exchange: at most five tool calls, a wall-clock
deadline for the whole thing, and a monthly rupee ceiling shared with the
categorisation path. Exceeding any of them ends the model's turn and the
deterministic answer stands in — which is possible at all because the
deterministic answer was already computed. That is the reason this design has
no failure mode worse than plainer wording.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.base import AIProvider, Message, ToolCall
from app.ai.prompts import ASSISTANT_SYSTEM_INSTRUCTION, assistant_context
from app.ai.router import get_provider
from app.assistant import deterministic, executor, tools as tool_registry, traceability
from app.assistant.period import month_label
from app.core.config import settings
from app.core.logging import get_logger
from app.intelligence import analytics
from app.models.enums import PrivacyIncidentKind
from app.privacy import gateway
from app.privacy.output_validator import validate_prose

logger = get_logger(__name__)

#: The plan's cap. Five is enough for "compare food between April and August"
#: (one call) plus a follow-up the model decides it needs; a model still asking
#: on the sixth is looping.
MAX_TOOL_CALLS = 5

#: Long enough for a real question, short enough that a pasted document is not
#: one. The question is the user's own text, so it is not treated as hostile —
#: but it is still bounded.
MAX_QUESTION_LENGTH = 400


class Source:
    """Where the wording came from. Shown to the user, not inferred by them."""

    MODEL = "model"
    DETERMINISTIC = "deterministic"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class Card:
    """One tool result, as the browser will draw it. Exact figures, not rounded."""

    tool: str
    render: str
    headline: str
    data: dict[str, Any]
    filters: dict[str, Any] | None


@dataclass(slots=True)
class Answer:
    question: str
    text: str
    source: str
    cards: list[Card] = field(default_factory=list)
    #: Honest caveats. Shown beneath the answer, never omitted to make it read
    #: better.
    notes: list[str] = field(default_factory=list)
    tool_calls: int = 0
    model_name: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "answer": self.text,
            "source": self.source,
            "cards": [
                {
                    "tool": card.tool,
                    "render": card.render,
                    "headline": card.headline,
                    "data": card.data,
                    "filters": card.filters,
                }
                for card in self.cards
            ],
            "notes": self.notes,
            "tool_calls": self.tool_calls,
            "model_name": self.model_name,
        }


_UNAVAILABLE = (
    "I could not work out what you meant. Try one of the questions below, or "
    "ask about a category, a merchant, a month, or an amount."
)


def _card(result: tool_registry.ToolResult) -> Card:
    return Card(
        tool=result.name,
        render=result.render,
        headline=result.headline,
        data=result.display,
        filters=result.filters,
    )


def _withheld_note(results: list[tool_registry.ToolResult]) -> str | None:
    names = {name for result in results for name in result.withheld_merchants}
    if not names:
        return None
    return (
        f"{len(names)} payee names were not sent to the model, because they are "
        "not recognised businesses. They are shown in full below — the "
        "restriction applies to what left this system, not to what you can see."
    )


# --------------------------------------------------------------------------- #
# The deterministic answer — always computed, sometimes the only one
# --------------------------------------------------------------------------- #

async def _run_plan(
    session: AsyncSession, plan: deterministic.Plan, default_month: date
) -> tuple[Card, tool_registry.ToolResult] | None:
    execution = await executor.execute(
        session,
        name=plan.tool,
        arguments=plan.arguments,
        default_month=default_month,
    )
    if not execution.ok or execution.result is None:
        return None
    return _card(execution.result), execution.result


async def _deterministic_answer(
    session: AsyncSession,
    *,
    question: str,
    suggestion_id: str | None,
    default_month: date,
) -> Answer:
    """Answer with no model involved.

    Also the fallback for every failure on the model path, which is why it is
    computed first rather than kept in reserve.
    """
    plan = (
        deterministic.plan_for_suggestion(suggestion_id, default_month=default_month)
        if suggestion_id
        else await deterministic.plan(session, question, default_month=default_month)
    )
    if plan is None:
        return Answer(question=question, text=_UNAVAILABLE, source=Source.UNAVAILABLE)

    ran = await _run_plan(session, plan, default_month)
    if ran is None:
        return Answer(question=question, text=_UNAVAILABLE, source=Source.UNAVAILABLE)

    card, _ = ran
    return Answer(
        question=question,
        text=card.headline,
        source=Source.DETERMINISTIC,
        cards=[card],
        tool_calls=1,
    )


# --------------------------------------------------------------------------- #
# The model path
# --------------------------------------------------------------------------- #

def _payload_hash(question: str, calls: list[str]) -> str:
    """Identifies a repeated exchange without storing what was asked."""
    body = json.dumps({"q": question, "tools": calls}, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


async def _ask_model(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    question: str,
    default_month: date,
    provider: AIProvider,
    fallback: Answer,
) -> Answer:
    """Run the tool loop, then check what came back."""
    model = settings.AI_MODEL_ASSISTANT
    deadline = time.monotonic() + settings.AI_ASSISTANT_TIMEOUT_SECONDS

    system_instruction = (
        ASSISTANT_SYSTEM_INSTRUCTION
        + "\n\n"
        + assistant_context(
            current_month_label=month_label(default_month),
            current_month=default_month.strftime("%Y-%m"),
        )
    )

    messages: list[Message] = [Message(role="user", text=question)]
    results: list[tool_registry.ToolResult] = []
    called: list[str] = []
    input_tokens = output_tokens = 0
    latency_ms = 0
    cost = Decimal("0")

    #: Outcomes that mean the model answered and we threw the answer away.
    #: A provider error is not one of them — nothing came back to reject, and
    #: counting it as a rejection would overstate how often the model misbehaves
    #: on the Privacy Center's own figures.
    rejected_outcomes = {gateway.Outcome.OUTPUT_REJECTED, gateway.Outcome.UNTRACEABLE}

    async def finish(outcome: str, *, error_code: str | None = None) -> None:
        await gateway.record_usage(
            session,
            tenant_id=tenant_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_inr=cost,
            outputs_rejected=1 if outcome in rejected_outcomes else 0,
        )
        await gateway.record_generation(
            session,
            tenant_id=tenant_id,
            purpose="assistant",
            model_name=model,
            model_version=None,
            payload_hash=_payload_hash(question, called),
            fields_sent=called,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_inr=cost,
            latency_ms=latency_ms,
            outcome=outcome,
            error_code=error_code,
        )

    for _ in range(MAX_TOOL_CALLS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.warning("assistant_deadline", stage="assistant", error_code="timeout")
            await finish(gateway.Outcome.PROVIDER_ERROR, error_code="timeout")
            return fallback

        try:
            turn = await asyncio.wait_for(
                provider.converse(
                    system_instruction=system_instruction,
                    messages=messages,
                    declarations=tool_registry.declarations(),
                    model=model,
                    timeout_seconds=int(min(remaining, settings.AI_TIMEOUT_SECONDS)),
                ),
                # Enforced here rather than trusted to the vendor client: a
                # timeout the transport does not honour is not a timeout.
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            logger.warning("assistant_deadline", stage="assistant", error_code="timeout")
            await finish(gateway.Outcome.PROVIDER_ERROR, error_code="timeout")
            return fallback

        # Usage accrues even on a turn we end up rejecting: the call was made
        # and it cost money, and a counter that only counts successes is not a
        # spend figure.
        input_tokens += turn.input_tokens
        output_tokens += turn.output_tokens
        latency_ms += turn.latency_ms
        cost += provider.cost_for(model, turn.input_tokens, turn.output_tokens)

        # Counted per round trip, whether or not the round trip was useful:
        # a spend figure that only counts successes is not a spend figure.
        await gateway.record_usage(session, tenant_id=tenant_id, calls=1)

        if turn.error_code is not None:
            logger.warning(
                "assistant_provider_error", stage="assistant", error_code=turn.error_code
            )
            await finish(gateway.Outcome.PROVIDER_ERROR, error_code=turn.error_code)
            return fallback

        if turn.tool_calls:
            if len(called) + len(turn.tool_calls) > MAX_TOOL_CALLS:
                logger.warning(
                    "assistant_tool_budget", stage="assistant", error_code="tool_budget"
                )
                await finish(gateway.Outcome.TOOL_BUDGET)
                return fallback

            messages.append(Message(role="model", tool_calls=turn.tool_calls))
            for call in turn.tool_calls:
                await _handle_call(
                    session,
                    call=call,
                    default_month=default_month,
                    messages=messages,
                    results=results,
                    called=called,
                )
            continue

        # No tool call: this is the answer.
        prose = validate_prose(turn.text)
        if not prose.ok:
            logger.warning(
                "assistant_output_rejected",
                stage="assistant",
                error_code=prose.rejected_by,
            )
            await gateway.record_incident(
                session,
                tenant_id=tenant_id,
                kind=(
                    PrivacyIncidentKind.OUTPUT_PII_ECHO
                    if prose.rejected_by == "pii_echo"
                    else PrivacyIncidentKind.OUTPUT_SCHEMA_VIOLATION
                ),
                detector=prose.detector or prose.rejected_by,
                context={"stage": "assistant_output"},
            )
            await finish(gateway.Outcome.OUTPUT_REJECTED, error_code=prose.rejected_by)
            return _fallback_with_cards(fallback, results, reason="rejected")

        allowed = traceability.allowed_figures(
            [result.model_view for result in results],
            [result.arguments for result in results],
        )
        trace = traceability.check(prose.text or "", allowed)
        if not trace.ok:
            # The single most important branch in this module. The wording is
            # discarded, not annotated — see traceability.py.
            logger.warning(
                "assistant_untraceable_figure",
                stage="assistant",
                error_code="untraceable_figure",
                count=len(trace.findings),
            )
            await gateway.record_incident(
                session,
                tenant_id=tenant_id,
                kind=PrivacyIncidentKind.OUTPUT_UNTRACEABLE_FIGURE,
                detector=",".join(trace.kinds),
                context={"stage": "assistant_output", "count": len(trace.findings)},
            )
            await finish(gateway.Outcome.UNTRACEABLE, error_code="untraceable_figure")
            return _fallback_with_cards(fallback, results, reason="untraceable")

        await finish(gateway.Outcome.OK)

        notes: list[str] = []
        withheld = _withheld_note(results)
        if withheld:
            notes.append(withheld)

        return Answer(
            question=question,
            text=prose.text or "",
            source=Source.MODEL,
            cards=[_card(result) for result in results],
            notes=notes,
            tool_calls=len(called),
            model_name=model,
        )

    await finish(gateway.Outcome.TOOL_BUDGET)
    return fallback


async def _handle_call(
    session: AsyncSession,
    *,
    call: ToolCall,
    default_month: date,
    messages: list[Message],
    results: list[tool_registry.ToolResult],
    called: list[str],
) -> None:
    """Run one requested tool and append its result to the transcript."""
    execution = await executor.execute(
        session,
        name=call.name,
        arguments=call.arguments,
        default_month=default_month,
    )
    called.append(call.name)

    if not execution.ok or execution.result is None:
        messages.append(
            Message(
                role="tool",
                tool_name=call.name,
                tool_result={"error": execution.error_message},
            )
        )
        return

    results.append(execution.result)
    messages.append(
        Message(
            role="tool",
            tool_name=call.name,
            tool_result=execution.result.model_view,
        )
    )


def _fallback_with_cards(
    fallback: Answer, results: list[tool_registry.ToolResult], *, reason: str
) -> Answer:
    """The deterministic answer, keeping whatever the model did legitimately fetch.

    The tool results are sound — they were computed here. Only the wording was
    rejected, so the cards stay and the sentence is replaced.
    """
    note = {
        "untraceable": (
            "The generated wording quoted a figure that did not come from your "
            "ledger, so it was discarded. This answer is written directly from "
            "the figures."
        ),
        "rejected": (
            "The generated wording did not pass the output checks, so it was "
            "discarded. This answer is written directly from the figures."
        ),
    }[reason]

    cards = [_card(result) for result in results] or fallback.cards
    text = cards[0].headline if cards else fallback.text

    return Answer(
        question=fallback.question,
        text=text,
        source=Source.DETERMINISTIC,
        cards=cards,
        notes=[note, *(n for n in [_withheld_note(results)] if n)],
        tool_calls=fallback.tool_calls,
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

async def answer(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    question: str,
    suggestion_id: str | None = None,
    provider: AIProvider | None = None,
) -> Answer:
    """Answer one question about this tenant's own money."""
    question = (question or "").strip()[:MAX_QUESTION_LENGTH]
    default_month = await analytics.default_month(session)

    baseline = await _deterministic_answer(
        session,
        question=question,
        suggestion_id=suggestion_id,
        default_month=default_month,
    )

    # A one-tap question is answered by the tool it advertises, without a model.
    # Its result is already a sentence, so a generation call would add latency,
    # cost and a chance of being wrong in exchange for nothing.
    if suggestion_id:
        return baseline

    if not settings.ai_usable:
        return baseline

    if not await gateway.within_budget(session):
        logger.info("assistant_budget_exceeded", stage="assistant", error_code="budget")
        return baseline

    engine = provider or get_provider()
    if not engine.available():
        return baseline

    return await _ask_model(
        session,
        tenant_id=tenant_id,
        question=question,
        default_month=default_month,
        provider=engine,
        fallback=baseline,
    )
