"""Unit tests for ElevenLabsTTSProvider (voice/providers/elevenlabs_tts.py).

These exercise the provider's own logic (message framing, fallback/retry
behavior, connection reuse across calls, WAV wrapping) against a fake
WebSocket -- no network, no API key required, so they run on every
machine/CI. Tests that would require the real ElevenLabs service are
skipped when ELEVENLABS_API_KEY is not set in the environment (mirroring
tests/test_sapi_tts_provider.py's skip-when-unavailable pattern).
"""
from __future__ import annotations

import base64
import json
import os

import pytest


def _audio_message(text: str, *, final: bool = False) -> str:
    return json.dumps({"audio": base64.b64encode(text.encode()).decode(), "isFinal": final})


class _FakeWebSocket:
    """Records every sent message and replays a scripted list of incoming
    ones -- enough to drive ElevenLabsTTSProvider's read loop without a
    real connection."""

    def __init__(self, incoming: list[str]) -> None:
        self.sent: list[dict] = []
        self._incoming = list(incoming)
        self.closed = False

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self) -> str:
        if not self._incoming:
            raise AssertionError("_FakeWebSocket.recv() called with nothing scripted")
        return self._incoming.pop(0)

    async def close(self) -> None:
        self.closed = True


def _provider():
    from voice.providers.elevenlabs_tts import ElevenLabsTTSProvider

    return ElevenLabsTTSProvider(api_key="test-key", voice_id="test-voice")


async def test_is_available_requires_both_key_and_voice_id():
    from voice.providers.elevenlabs_tts import ElevenLabsTTSProvider

    assert ElevenLabsTTSProvider(api_key=None, voice_id="v").is_available is False
    assert ElevenLabsTTSProvider(api_key="k", voice_id=None).is_available is False
    assert ElevenLabsTTSProvider(api_key="k", voice_id="v").is_available is True


async def test_synthesize_raises_when_not_configured():
    from voice.errors import VoiceProviderError
    from voice.providers.elevenlabs_tts import ElevenLabsTTSProvider

    provider = ElevenLabsTTSProvider(api_key=None, voice_id=None)
    with pytest.raises(VoiceProviderError):
        await provider.synthesize("hello")


async def test_synthesize_wraps_streamed_pcm_as_wav(monkeypatch):
    provider = _provider()
    fake_ws = _FakeWebSocket([_audio_message("abcd", final=True)])

    async def _fake_ensure_connected_locked():
        provider._ws = fake_ws

    monkeypatch.setattr(provider, "_ensure_connected_locked", _fake_ensure_connected_locked)

    result = await provider.synthesize("hello there")

    assert result.audio.startswith(b"RIFF")
    assert b"WAVE" in result.audio[:16]
    assert result.sample_rate == provider._sample_rate
    # synthesize() always closes its own one-shot connection.
    assert fake_ws.closed is True
    # First real content message carries the text with a flush, so the
    # generation boundary is this chunk, not buffered indefinitely.
    text_messages = [m for m in fake_ws.sent if m.get("text")]
    assert text_messages[0]["flush"] is True
    assert "hello there" in text_messages[0]["text"]


async def test_stream_chunk_reuses_one_connection_across_calls(monkeypatch):
    provider = _provider()
    connect_calls = 0
    fake_ws = _FakeWebSocket(
        [
            _audio_message("first", final=True),
            _audio_message("second", final=True),
        ]
    )

    async def _fake_ensure_connected_locked():
        nonlocal connect_calls
        if provider._ws is None:
            connect_calls += 1
            provider._ws = fake_ws

    monkeypatch.setattr(provider, "_ensure_connected_locked", _fake_ensure_connected_locked)

    chunks_a = [pcm async for pcm in provider.stream_chunk("Sentence one.")]
    chunks_b = [pcm async for pcm in provider.stream_chunk("Sentence two.")]

    assert chunks_a == [b"first"]
    assert chunks_b == [b"second"]
    assert connect_calls == 1  # NOT reconnected for the second sentence
    assert fake_ws.closed is False  # stream_chunk never closes on its own

    await provider.end_turn()
    assert fake_ws.closed is True


async def test_stream_chunk_raises_and_closes_on_timeout(monkeypatch):
    from voice.errors import VoiceProviderError

    provider = _provider()
    fake_ws = _FakeWebSocket([])  # recv() will be replaced to time out below

    async def _fake_ensure_connected_locked():
        provider._ws = fake_ws

    async def _timeout_recv():
        raise TimeoutError()

    monkeypatch.setattr(provider, "_ensure_connected_locked", _fake_ensure_connected_locked)
    monkeypatch.setattr(fake_ws, "recv", _timeout_recv)

    with pytest.raises(VoiceProviderError):
        async for _ in provider.stream_chunk("hello"):
            pass

    # A failed chunk must not leave a half-open connection behind -- the
    # next call reconnects from scratch rather than reusing a dead socket.
    assert provider._ws is None


async def test_stream_chunk_treats_quiet_stream_after_audio_as_chunk_done(monkeypatch):
    """isFinal timing on a real flush is not verified against the live API
    (see the module docstring) -- a read timeout AFTER some audio already
    arrived must be treated as end-of-chunk, not an error, so a live
    protocol quirk fails safe instead of aborting a sentence that was
    already partially spoken."""
    provider = _provider()
    fake_ws = _FakeWebSocket([json.dumps({"audio": base64.b64encode(b"partial").decode()})])

    call_count = 0
    real_recv = fake_ws.recv

    async def _recv_then_timeout():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return await real_recv()
        raise TimeoutError()

    async def _fake_ensure_connected_locked():
        provider._ws = fake_ws

    monkeypatch.setattr(provider, "_ensure_connected_locked", _fake_ensure_connected_locked)
    monkeypatch.setattr(fake_ws, "recv", _recv_then_timeout)

    chunks = [pcm async for pcm in provider.stream_chunk("hello")]

    assert chunks == [b"partial"]


@pytest.mark.skipif(
    not os.environ.get("ELEVENLABS_API_KEY") or not os.environ.get("ELEVENLABS_VOICE_ID"),
    reason="requires a real ELEVENLABS_API_KEY/ELEVENLABS_VOICE_ID in the environment",
)
async def test_synthesize_produces_real_audio_against_the_live_api():
    from voice.providers.elevenlabs_tts import ElevenLabsTTSProvider

    provider = ElevenLabsTTSProvider(
        api_key=os.environ["ELEVENLABS_API_KEY"],
        voice_id=os.environ["ELEVENLABS_VOICE_ID"],
    )
    result = await provider.synthesize("This is a physical ElevenLabs latency test.")
    assert result.audio.startswith(b"RIFF")
    assert len(result.audio) > 44
