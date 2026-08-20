"""Builds the configured AssistantProvider from Settings. The single choke
point that knows every concrete adapter — nothing else in the backend
imports a concrete provider class directly."""
from __future__ import annotations

from app.config import Settings
from assistant.provider import AssistantProvider
from assistant.providers.claude_code import ClaudeCodeProvider
from assistant.providers.codex import CodexProvider
from assistant.providers.gemini import GeminiProvider
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
    if provider == "codex":
        return CodexProvider(
            command=settings.codex_command,
            timeout_seconds=settings.codex_timeout_seconds,
        )
    if provider == "gemini":
        return GeminiProvider(
            command=settings.gemini_command,
            timeout_seconds=settings.gemini_timeout_seconds,
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
        "one of: mock, claude_code, codex, gemini, ollama, anthropic_api"
    )


def build_chart_assistant_provider(settings: Settings) -> AssistantProvider:
    """Chart analysis always uses ClaudeCodeProvider specifically (it's the
    only adapter here that can look at an image, via its own Read tool),
    regardless of which provider ASSISTANT_PROVIDER configures for ordinary
    chat. Still the same AssistantProvider interface -- not a second
    provider framework, just a second choke-point call for a feature that
    has a fixed adapter requirement."""
    return ClaudeCodeProvider(
        command=settings.claude_code_command,
        timeout_seconds=settings.chart_analysis_timeout_seconds,
    )
