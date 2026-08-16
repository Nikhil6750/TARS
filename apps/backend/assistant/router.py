"""Deterministic-vs-model routing, per ARCHITECTURE.md § Assistant
architecture: deterministic command/state requests resolve in deterministic
code and never touch a model call. Only queries that don't match a
deterministic intent reach the configured AssistantProvider, and even then
with deterministic state as grounding (assistant/grounding.py) — never as
the sole source of trading facts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from opentelemetry.trace import Status, StatusCode

from app.observability import get_tracer
from app.schemas import (
    AssistantMessage,
    InputMode,
    MessageProviders,
    MessageRole,
)
from assistant.conversation_store import ConversationStore
from assistant.errors import AssistantProviderError
from assistant.grounding import build_system_context
from assistant.provider import AssistantProvider, AssistantRequest
from events.service import EventService

if TYPE_CHECKING:
    from memory.service import MemoryService

tracer = get_tracer()

# Deterministic intents are matched by keyword, on purpose: this is the
# resolver ARCHITECTURE.md means by "deterministic code", not a fuzzy
# classifier — false negatives fall through to the model (safe, just less
# snappy); false positives would wrongly skip the model, so keep these
# patterns narrow and specific rather than broad.
_ACTIVE_SETUPS_PATTERN = re.compile(
    r"\bactive\s+setups?\b|\bwhat'?s\s+active\b|\bshow\s+active\b", re.IGNORECASE
)
_ATTENTION_PATTERN = re.compile(
    r"\battention\b|\bneeds?\s+(my\s+)?attention\b|\brequires?\s+(my\s+)?attention\b",
    re.IGNORECASE,
)


@dataclass
class RouterReply:
    conversation_id: str
    user_message: AssistantMessage
    assistant_message: AssistantMessage


class AssistantRouter:
    def __init__(
        self,
        event_service: EventService,
        conversation_store: ConversationStore,
        provider: AssistantProvider,
        memory_service: MemoryService | None = None,
    ):
        self._events = event_service
        self._conversations = conversation_store
        self._provider = provider
        self._memory = memory_service

    async def _save(self, message: AssistantMessage) -> None:
        await self._conversations.save(message)
        if self._memory is not None:
            await self._memory.index_conversation_message(message)

    async def handle_text(self, text: str, conversation_id: str | None) -> RouterReply:
        conversation_id = conversation_id or str(uuid4())

        user_message = AssistantMessage(
            conversation_id=UUID(conversation_id),
            role=MessageRole.user,
            content=text,
            input_mode=InputMode.text,
        )
        await self._save(user_message)

        intent, deterministic_text = await self._try_deterministic(text)
        if deterministic_text is not None:
            assistant_message = AssistantMessage(
                conversation_id=UUID(conversation_id),
                role=MessageRole.assistant,
                content=deterministic_text,
                input_mode=InputMode.text,
                intent=intent,
                providers=MessageProviders(assistant="deterministic"),
            )
        else:
            assistant_message = await self._call_provider(text, conversation_id)

        await self._save(assistant_message)
        return RouterReply(
            conversation_id=conversation_id,
            user_message=user_message,
            assistant_message=assistant_message,
        )

    async def _try_deterministic(self, text: str) -> tuple[str | None, str | None]:
        if _ACTIVE_SETUPS_PATTERN.search(text):
            active = await self._events.get_active_setups()
            return "show_active_setups", _format_active_setups(active)
        if _ATTENTION_PATTERN.search(text):
            active = await self._events.get_active_setups()
            warnings = await self._events.get_recent_warnings(limit=5)
            return "attention_summary", _format_attention_summary(active, warnings)
        return None, None

    async def _call_provider(self, text: str, conversation_id: str) -> AssistantMessage:
        active = await self._events.get_active_setups()
        history_rows = await self._conversations.get_recent(conversation_id, limit=10)
        history = [
            {"role": row["role"], "content": row["content"]}
            for row in history_rows
            if row["role"] in ("user", "assistant")
        ]

        request = AssistantRequest(
            text=text,
            conversation_id=conversation_id,
            system_context=build_system_context(active),
            history=history,
        )
        with tracer.start_as_current_span("assistant.request") as span:
            span.set_attribute("assistant.provider", self._provider.name)
            span.set_attribute("assistant.history_turns", len(history))
            span.set_attribute("assistant.grounded_setups", len(active))
            try:
                reply = await self._provider.respond(request)
            except AssistantProviderError as exc:
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                span.record_exception(exc)
                return AssistantMessage(
                    conversation_id=UUID(conversation_id),
                    role=MessageRole.assistant,
                    content=(
                        "I couldn't reach the configured assistant provider "
                        f"({self._provider.name}) to answer that."
                    ),
                    input_mode=InputMode.text,
                    providers=MessageProviders(assistant=self._provider.name),
                    error=str(exc),
                )

        return AssistantMessage(
            conversation_id=UUID(conversation_id),
            role=MessageRole.assistant,
            content=reply.text,
            input_mode=InputMode.text,
            providers=MessageProviders(assistant=reply.provider),
        )


def _format_active_setups(active: list[dict[str, Any]]) -> str:
    if not active:
        return "There are no active setups right now."
    lines = ["Active setups:"]
    for event in active:
        parts = [f"{event['symbol']} ({event['state']}"]
        if event.get("direction"):
            parts.append(f", {event['direction']}")
        parts.append(")")
        line = "".join(parts)
        if event.get("entry") is not None:
            line += f" — entry {event['entry']}"
        if event.get("stop_loss") is not None:
            line += f", SL {event['stop_loss']}"
        if event.get("take_profit") is not None:
            line += f", TP {event['take_profit']}"
        if event.get("risk_reward") is not None:
            line += f", R:R {event['risk_reward']}"
        lines.append(f"- {line}")
    return "\n".join(lines)


def _format_attention_summary(
    active: list[dict[str, Any]], warnings: list[dict[str, Any]]
) -> str:
    lines: list[str] = []
    valid = [e for e in active if e.get("validation_status") == "VALID"]
    if valid:
        symbols = ", ".join(e["symbol"] for e in valid)
        lines.append(f"Valid active setups: {symbols}.")
    if warnings:
        lines.append("Recent warnings:")
        for w in warnings:
            text = "; ".join(w.get("warnings") or []) or w["state"]
            lines.append(f"- {w['symbol']}: {text}")
    if not lines:
        return "Nothing currently requires your attention."
    return "\n".join(lines)
