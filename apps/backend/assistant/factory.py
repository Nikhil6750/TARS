"""Builds the configured AssistantProvider from Settings. The single choke
point that knows every concrete adapter — nothing else in the backend
imports a concrete provider class directly."""
from __future__ import annotations

from app.config import Settings
from assistant.provider import AssistantProvider
from assistant.providers.claude_code import ClaudeCodeProvider
from assistant.providers.mock import MockAssistantProvider
from assistant.providers.ollama import OllamaProvider


def build_assistant_provider(settings: Settings) -> AssistantProvider:
    provider = settings.assistant_provider.lower()

    if provider == "mock":
        return MockAssistantProvider()
    if provider == "claude_code":
        return ClaudeCodeProvider(
            command=settings.claude_code_command,
            timeout_seconds=settings.claude_code_timeout_seconds,
        )
    if provider == "ollama":
        return OllamaProvider(
            base_url=settings.ollama_base_url, model=settings.ollama_model or ""
        )
    if provider == "anthropic_api":
        from assistant.providers.anthropic_api import AnthropicAPIProvider

        return AnthropicAPIProvider(
            api_key=settings.anthropic_api_key or "", model=settings.anthropic_model
        )

    raise ValueError(
        f"Unknown ASSISTANT_PROVIDER '{settings.assistant_provider}' — expected "
        "one of: mock, claude_code, ollama, anthropic_api"
    )
