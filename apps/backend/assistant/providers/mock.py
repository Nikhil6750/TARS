"""Offline, zero-dependency AssistantProvider. Guarantees the backend
answers *something* with no model, no API key, and no network — the
free/local-first floor per AGENTS.md."""
from __future__ import annotations

from assistant.provider import AssistantProvider, AssistantReply, AssistantRequest


class MockAssistantProvider(AssistantProvider):
    name = "mock"

    async def respond(self, request: AssistantRequest) -> AssistantReply:
        if request.system_context:
            text = (
                "This is a canned response from the mock assistant provider "
                "(no LLM configured). I can see current TARS state was "
                "provided as context, but I have no language model to "
                "reason over it with."
            )
        else:
            text = (
                "This is a canned response from the mock assistant provider "
                "(no LLM configured)."
            )
        return AssistantReply(text=text, provider=self.name)
