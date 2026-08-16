"""Offline, zero-dependency AssistantProvider. Guarantees the backend
answers *something* with no model, no API key, and no network — the
free/local-first floor per AGENTS.md."""
from __future__ import annotations

from assistant.provider import AssistantProvider, AssistantReply, AssistantRequest


class MockAssistantProvider(AssistantProvider):
    name = "mock"

    async def respond(self, request: AssistantRequest) -> AssistantReply:
        text = (
            "This is a canned response from the mock assistant provider "
            "(no LLM configured). The requested trading data is unavailable in current state (no active data)."
        )
        return AssistantReply(text=text, provider=self.name)
