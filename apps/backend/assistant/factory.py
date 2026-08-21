"""Builds the configured AssistantProvider from Settings. The single choke
point that knows every concrete adapter — nothing else in the backend
imports a concrete provider class directly."""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.config import Settings
from assistant.provider import AssistantProvider
from assistant.provider_router import RoutedAssistantProvider
from assistant.providers.claude_code import ClaudeCodeProvider
from assistant.providers.codex import CodexProvider
from assistant.providers.gemini import GeminiProvider
from assistant.providers.mock import MockAssistantProvider
from assistant.providers.ollama import OllamaProvider

if TYPE_CHECKING:
    from app.latency_store import LatencyTraceStore


def build_assistant_provider(
    settings: Settings,
    trace_store: LatencyTraceStore | None = None,
) -> AssistantProvider:
    provider = settings.assistant_provider.lower()

    if provider == "mock":
        return MockAssistantProvider()
    if provider == "claude_code":
        claude_primary = ClaudeCodeProvider(
            command=settings.claude_code_command,
            timeout_seconds=settings.claude_code_timeout_seconds,
            persist_sessions=False,
        )
        return _route_cli_pool(claude_primary, settings, trace_store)
    if provider == "codex":
        codex_primary = CodexProvider(
            command=settings.codex_command,
            timeout_seconds=settings.codex_timeout_seconds,
        )
        return _route_cli_pool(codex_primary, settings, trace_store)
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
    if provider == "auto":
        candidates: list[AssistantProvider] = []
        if settings.ollama_model:
            candidates.append(
                OllamaProvider(base_url=settings.ollama_base_url, model=settings.ollama_model)
            )
        candidates.extend(
            (
                ClaudeCodeProvider(
                    command=settings.claude_code_command,
                    timeout_seconds=settings.claude_code_timeout_seconds,
                    persist_sessions=False,
                ),
                CodexProvider(
                    command=settings.codex_command,
                    timeout_seconds=settings.codex_timeout_seconds,
                ),
            )
        )
        available = [candidate for candidate in candidates if getattr(candidate, "is_available", True)]
        if not available:
            return MockAssistantProvider()
        return RoutedAssistantProvider(available, trace_store=trace_store)

    raise ValueError(
        f"Unknown ASSISTANT_PROVIDER '{settings.assistant_provider}' — expected "
        "one of: mock, auto, claude_code, codex, gemini, ollama, anthropic_api"
    )


def _route_cli_pool(
    primary: AssistantProvider,
    settings: Settings,
    trace_store: LatencyTraceStore | None,
) -> AssistantProvider:
    """Keep direct factory compatibility, route in the stateful application."""

    if trace_store is None:
        return primary
    alternate: AssistantProvider
    if primary.name == "claude_code":
        alternate = CodexProvider(
            command=settings.codex_command,
            timeout_seconds=settings.codex_timeout_seconds,
        )
    else:
        alternate = ClaudeCodeProvider(
            command=settings.claude_code_command,
            timeout_seconds=settings.claude_code_timeout_seconds,
            persist_sessions=False,
        )
    candidates = [primary]
    if getattr(alternate, "is_available", True):
        candidates.append(alternate)
    if settings.ollama_model:
        candidates.append(OllamaProvider(settings.ollama_base_url, settings.ollama_model))
    return RoutedAssistantProvider(candidates, trace_store=trace_store)


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
        persist_sessions=False,
    )
