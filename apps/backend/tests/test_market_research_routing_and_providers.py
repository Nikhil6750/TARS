"""Tests for Market Research routing, general context separation, provider verification,
and strict non-fallback provider health.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from assistant.errors import AssistantProviderError
from assistant.factory import build_assistant_provider
from assistant.provider import (
    AssistantProvider,
    AssistantReply,
    AssistantRequest,
    ProviderDiagnostics,
)
from assistant.providers.claude_code import sanitize_user_facing_text
from assistant.providers.codex import CodexProvider
from assistant.providers.gemini import GeminiProvider
from intelligence.market_research import (
    MarketResearchEngine,
)
from intelligence.router import IntelligenceRouter, IntentKind


class _DummyProvider(AssistantProvider):
    name = "dummy"

    def __init__(self, reply_text: str = "Synthesized research output") -> None:
        self.reply_text = reply_text
        self.last_request: AssistantRequest | None = None

    async def respond(self, request: AssistantRequest) -> AssistantReply:
        self.last_request = request
        return AssistantReply(
            text=self.reply_text,
            provider=self.name,
            diagnostics=ProviderDiagnostics(
                provider_id=self.name,
                model="dummy-model",
                exit_code=0,
                fallback_used=False,
            ),
        )


MANDATORY_MARKET_RESEARCH_QUERIES = [
    "is there any news today that can affect XAUUSD",
    "is there any news today that can affect the XAUUSD",
    "what news is affecting gold today",
    "research today's macro drivers for EURUSD",
    "what economic events could move XAUUSD today",
    "what is the current market sentiment",
    "what is affecting the dollar today",
    "research current Fed and ECB drivers",
    "any important news before I trade gold",
]


@pytest.mark.parametrize("query", MANDATORY_MARKET_RESEARCH_QUERIES)
def test_market_research_intent_classification(query: str):
    router = IntelligenceRouter(provider=_DummyProvider())
    intent = router.classify_intent(query)
    assert intent == IntentKind.MARKET_RESEARCH, f"Query '{query}' classified as {intent}, expected MARKET_RESEARCH"


def test_educational_query_classification():
    assert MarketResearchEngine.is_educational_query("what generally affects XAUUSD?")
    assert MarketResearchEngine.is_educational_query("what affects gold generally")
    assert MarketResearchEngine.is_educational_query("historically what moves the dollar")
    assert not MarketResearchEngine.is_educational_query("is there any news today that can affect XAUUSD")


@pytest.mark.asyncio
async def test_unavailable_live_feed_returns_clean_unavailable_message():
    engine = MarketResearchEngine(provider=_DummyProvider(), retrieval_provider=None, memory_service=None)
    report = await engine.generate_research(
        query="is there any news today that can affect XAUUSD",
        conversation_id="conv-1",
    )
    assert not report.is_available
    assert "CURRENT RESEARCH UNAVAILABLE" in report.content
    assert "Live market/news retrieval is not currently connected." in report.content
    assert "My web search tool was blocked" not in report.content
    assert report.provider == "deterministic"


@pytest.mark.asyncio
async def test_educational_query_returns_separated_general_context():
    engine = MarketResearchEngine(provider=_DummyProvider(), retrieval_provider=None, memory_service=None)
    report = await engine.generate_research(
        query="what generally affects XAUUSD?",
        conversation_id="conv-2",
    )
    assert not report.is_available
    assert "GENERAL CONTEXT" in report.content
    assert "Unavailable — live feed is not connected." in report.content
    assert "General Educational Drivers:" in report.content
    assert "Note: This is general educational context, not today's live market drivers or current news." in report.content


@pytest.mark.asyncio
async def test_market_research_synthesizes_when_real_evidence_exists():
    mock_retrieval = AsyncMock()
    mock_retrieval.retrieve.return_value = [
        {
            "source": "FRED",
            "url": "https://fred.stlouisfed.org/series/DFII10",
            "retrieval_timestamp": "2026-08-20T12:00:00Z",
            "publication_timestamp": "2026-08-20T11:00:00Z",
            "content": "US 10-Year Real Yield at 1.85%",
        }
    ]
    provider = _DummyProvider(reply_text="### MACRO DRIVERS\n• Real yields at 1.85% weigh on gold.")
    engine = MarketResearchEngine(
        provider=provider,
        retrieval_provider=mock_retrieval,
        memory_service=None,
    )
    report = await engine.generate_research(
        query="research today's macro drivers for EURUSD",
        conversation_id="conv-3",
    )
    assert report.is_available
    assert report.provider == "dummy"
    assert "Real yields at 1.85%" in report.content
    assert len(report.evidence_objects) == 1
    assert report.evidence_objects[0].source == "FRED"


def test_sanitize_user_facing_text_removes_cli_leakage():
    leaked_1 = "My web search tool was blocked — the permission prompt wasn't granted. Gold is typically driven by yields."
    cleaned_1 = sanitize_user_facing_text(leaked_1)
    assert "My web search tool was blocked" not in cleaned_1
    assert "permission prompt wasn't granted" not in cleaned_1
    assert "Gold is typically driven by yields." in cleaned_1

    leaked_2 = "Claude permission wasn't granted. Real yields affect gold."
    cleaned_2 = sanitize_user_facing_text(leaked_2)
    assert "Claude permission" not in cleaned_2
    assert "Real yields affect gold." in cleaned_2


@pytest.mark.asyncio
async def test_missing_codex_provider_fails_explicitly_without_silent_fallback():
    provider = CodexProvider(command="non_existent_codex_executable_xyz")
    assert not provider.is_available
    with pytest.raises(AssistantProviderError) as exc_info:
        await provider.respond(AssistantRequest(text="Reply with exactly: TARS_PROVIDER_OK", conversation_id="c1"))
    assert "Codex CLI" in str(exc_info.value)
    assert "not found on PATH" in str(exc_info.value)


@pytest.mark.asyncio
async def test_missing_gemini_provider_fails_explicitly_without_silent_fallback():
    provider = GeminiProvider(command="non_existent_gemini_executable_xyz")
    assert not provider.is_available
    with pytest.raises(AssistantProviderError) as exc_info:
        await provider.respond(AssistantRequest(text="Reply with exactly: TARS_PROVIDER_OK", conversation_id="c1"))
    assert "Gemini CLI" in str(exc_info.value)
    assert "not found on PATH" in str(exc_info.value)


def test_provider_factory_builds_all_configured_providers(monkeypatch):
    from app.config import Settings

    settings_mock = Settings(assistant_provider="mock")
    p_mock = build_assistant_provider(settings_mock)
    assert p_mock.name == "mock"

    settings_claude = Settings(assistant_provider="claude_code")
    p_claude = build_assistant_provider(settings_claude)
    assert p_claude.name == "claude_code"

    settings_codex = Settings(assistant_provider="codex")
    p_codex = build_assistant_provider(settings_codex)
    assert p_codex.name == "codex"

    settings_gemini = Settings(assistant_provider="gemini")
    p_gemini = build_assistant_provider(settings_gemini)
    assert p_gemini.name == "gemini"

    with pytest.raises(ValueError, match="Unknown ASSISTANT_PROVIDER"):
        build_assistant_provider(Settings(assistant_provider="unknown_provider_xyz"))
