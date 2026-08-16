from __future__ import annotations

import io
import sys
import threading
import time
import wave
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def wav_bytes(pcm: bytes, sample_rate: int = 16_000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(pcm)
    return buffer.getvalue()


def test_audio_transcript_assistant_and_tts_use_actual_backend_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Controlled providers make the real backend voice plumbing deterministic."""

    transcript = "What setups require my attention?"
    conversation_id = "64dd4f95-76a1-4ca8-af48-c89d956a7ba4"
    recorded_pcm = b"\x01\x00\x02\x00\x03\x00\x04\x00"
    synthesized_wav = wav_bytes(b"\x05\x00\x06\x00")

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'voice.db'}")
    monkeypatch.setenv("USE_MOCK_TRADING_EVENTS", "false")
    monkeypatch.setenv("ASSISTANT_PROVIDER", "mock")
    monkeypatch.setenv("STT_PROVIDER", "mock")
    monkeypatch.setenv("TTS_PROVIDER", "mock")
    monkeypatch.setenv("WAKE_WORD_PROVIDER", "mock")
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(tmp_path / "vault"))

    from app.config import get_settings
    from app.main import create_app
    from fastapi.testclient import TestClient
    from voice.interfaces import (
        SpeechToTextProvider,
        SynthesisResult,
        TextToSpeechProvider,
        TranscriptionResult,
    )

    class RecordingSTT(SpeechToTextProvider):
        name = "certification_stt"
        sample_rate = 16_000

        def __init__(self) -> None:
            self.received: list[bytes] = []

        async def transcribe(self, pcm_audio: bytes) -> TranscriptionResult:
            self.received.append(pcm_audio)
            return TranscriptionResult(text=transcript, language="en")

    class RecordingTTS(TextToSpeechProvider):
        name = "certification_tts"
        sample_rate = 16_000

        def __init__(self) -> None:
            self.received: list[str] = []

        async def synthesize(self, text: str) -> SynthesisResult:
            self.received.append(text)
            return SynthesisResult(audio=synthesized_wav, sample_rate=self.sample_rate)

    get_settings.cache_clear()
    app = create_app()
    stt = RecordingSTT()
    tts = RecordingTTS()
    try:
        with TestClient(app) as api:
            deadline = time.monotonic() + 5.0
            while not api.get("/api/v1/voice/status").json()["ready"]:
                assert time.monotonic() < deadline, "voice providers did not become ready"
                threading.Event().wait(0.01)
            app.state.voice_providers.stt = stt
            app.state.voice_providers.tts = tts

            transcription = api.post(
                "/api/v1/voice/transcribe",
                files={"file": ("recording.wav", wav_bytes(recorded_pcm), "audio/wav")},
            )
            transcription.raise_for_status()
            assert transcription.json()["text"] == transcript
            assert stt.received == [recorded_pcm]

            assistant = api.post(
                "/api/v1/assistant/query",
                json={
                    "text": transcription.json()["text"],
                    "conversation_id": conversation_id,
                },
            )
            assistant.raise_for_status()
            assistant_text = assistant.json()["content"]
            assert assistant.json()["intent"] == "attention_summary"
            assert "nothing currently requires your attention" in assistant_text.casefold()

            synthesis = api.post(
                "/api/v1/voice/synthesize", json={"text": assistant_text}
            )
            synthesis.raise_for_status()
            assert synthesis.content == synthesized_wav
            assert synthesis.headers["content-type"].startswith("audio/wav")
            assert tts.received == [assistant_text]
    finally:
        get_settings.cache_clear()
