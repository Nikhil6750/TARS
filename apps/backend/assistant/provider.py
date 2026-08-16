"""AssistantProvider — the single interface the rest of the backend talks
to, per ARCHITECTURE.md § Assistant architecture. Concrete adapters
(ClaudeCodeProvider, OllamaProvider, AnthropicAPIProvider, MockProvider)
implement this; nothing outside assistant/ imports a concrete adapter
directly.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class AssistantRequest:
    text: str
    conversation_id: str
    # Deterministic TARS state, pre-formatted as grounding text. Empty
    # string when there is nothing to ground (e.g. no active setups). The
    # provider is instructed to treat this as the only source of trading
    # facts — never to invent beyond it.
    system_context: str = ""
    # Prior turns for this conversation, oldest first, as
    # {"role": "user"|"assistant", "content": str} dicts.
    history: list[dict] = field(default_factory=list)


@dataclass
class AssistantReply:
    text: str
    provider: str


class AssistantProvider(ABC):
    name: str

    @abstractmethod
    async def respond(self, request: AssistantRequest) -> AssistantReply:
        """Raises AssistantProviderError on any failure to produce a reply."""
