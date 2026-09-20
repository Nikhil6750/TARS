from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from assistant.errors import AssistantProviderError
from assistant.provider import AssistantRequest
from assistant.providers.gemini_fast import GeminiFastConversationProvider


def test_is_available_false_without_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    provider = GeminiFastConversationProvider(api_key=None)
    assert provider.is_available is False


def test_is_available_true_with_explicit_api_key():
    with patch("google.genai.Client"):
        provider = GeminiFastConversationProvider(api_key="fake-key")
    assert provider.is_available is True


async def test_respond_returns_reply_text():
    with patch("google.genai.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_response = MagicMock()
        mock_response.text = "A Docker container is a lightweight, isolated unit."
        mock_client.models.generate_content.return_value = mock_response

        provider = GeminiFastConversationProvider(api_key="fake-key")
        reply = await provider.respond(
            AssistantRequest(text="Explain Docker containers.", conversation_id="c1")
        )

    assert reply.text == "A Docker container is a lightweight, isolated unit."
    assert reply.provider == "gemini_fast"


async def test_respond_raises_on_empty_text():
    with patch("google.genai.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_response = MagicMock()
        mock_response.text = ""
        mock_client.models.generate_content.return_value = mock_response

        provider = GeminiFastConversationProvider(api_key="fake-key")
        with pytest.raises(AssistantProviderError):
            await provider.respond(AssistantRequest(text="hi", conversation_id="c1"))


async def test_respond_wraps_client_exception():
    with patch("google.genai.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.models.generate_content.side_effect = RuntimeError("network exploded")

        provider = GeminiFastConversationProvider(api_key="fake-key")
        with pytest.raises(AssistantProviderError):
            await provider.respond(AssistantRequest(text="hi", conversation_id="c1"))


async def test_respond_without_api_key_raises_without_calling_client():
    provider = GeminiFastConversationProvider(api_key=None)
    with pytest.raises(AssistantProviderError):
        await provider.respond(AssistantRequest(text="hi", conversation_id="c1"))


async def test_respond_stream_yields_deltas_then_complete():
    def _fake_stream(**_kwargs):
        for text in ("A Docker ", "container is ", "lightweight."):
            chunk = MagicMock()
            chunk.text = text
            yield chunk

    with patch("google.genai.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.models.generate_content_stream.side_effect = _fake_stream

        provider = GeminiFastConversationProvider(api_key="fake-key")
        events = [
            event
            async for event in provider.respond_stream(
                AssistantRequest(text="Explain Docker containers.", conversation_id="c1")
            )
        ]

    deltas = [e for e in events if e["type"] == "delta"]
    completes = [e for e in events if e["type"] == "complete"]
    assert [d["text"] for d in deltas] == ["A Docker ", "container is ", "lightweight."]
    assert completes == [
        {"type": "complete", "text": "A Docker container is lightweight.", "provider": "gemini_fast"}
    ]


async def test_respond_stream_raises_on_empty_result():
    def _fake_empty_stream(**_kwargs):
        return iter(())

    with patch("google.genai.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.models.generate_content_stream.side_effect = _fake_empty_stream

        provider = GeminiFastConversationProvider(api_key="fake-key")
        with pytest.raises(AssistantProviderError):
            async for _event in provider.respond_stream(
                AssistantRequest(text="hi", conversation_id="c1")
            ):
                pass
