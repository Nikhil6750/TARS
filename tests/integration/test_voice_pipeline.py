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


def test_minimal_golden_loop_owns_every_required_voice_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
            self.text = ""
            self.received: list[bytes] = []

        async def transcribe(self, pcm_audio: bytes) -> TranscriptionResult:
            self.received.append(pcm_audio)
            return TranscriptionResult(text=self.text, language="en")

    class RecordingTTS(TextToSpeechProvider):
        name = "certification_tts"
        sample_rate = 16_000

        def __init__(self) -> None:
            self.received: list[str] = []

        async def synthesize(self, text: str) -> SynthesisResult:
            self.received.append(text)
            return SynthesisResult(
                audio=wav_bytes(b"\x05\x00\x06\x00"),
                sample_rate=self.sample_rate,
            )

    get_settings.cache_clear()
    app = create_app()
    stt = RecordingSTT()
    tts = RecordingTTS()
    pcm = b"\x01\x00\x02\x00\x03\x00\x04\x00"
    wav = wav_bytes(pcm)
    conversation_id = "64dd4f95-76a1-4ca8-af48-c89d956a7ba4"

    try:
        with TestClient(app) as api:
            deadline = time.monotonic() + 5
            while not api.get("/api/v1/voice/status").json()["ready"]:
                assert time.monotonic() < deadline
                threading.Event().wait(0.01)
            app.state.voice_providers.stt = stt
            app.state.voice_providers.tts = tts

            def utterance(transcript: str, turn_id: str, session_id: str = "golden") -> dict:
                stt.text = transcript
                response = api.post(
                    "/api/v1/voice/utterance",
                    files={"file": ("utterance.wav", wav, "audio/wav")},
                    data={
                        "turn_id": turn_id,
                        "conversation_id": conversation_id,
                        "session_id": session_id,
                        "audio_detected_at_ms": "1787382000000",
                        "speech_end_at_ms": "1787382000500",
                    },
                )
                response.raise_for_status()
                return response.json()

            wake = utterance("Hey TARS", "required-wake", "two-stage")
            assert wake["status"] == "awaiting_command"
            assert wake["speech_text"] == "Yeah?"
            assert wake["transcript"] == "Hey TARS"
            assert app.state.turn_controller.execution_count("required-wake") == 0

            continuation = utterance(
                "Explain Docker containers.", "required-two-stage-command", "two-stage"
            )
            assert continuation["intent"] == "NORMAL_CONVERSATION"
            assert continuation["status"] == "completed"
            assert continuation["audio_chunks_base64"]

            required = (
                ("Hey TARS what time is it", "required-time", "DETERMINISTIC"),
                ("Hey TARS explain polymorphism", "required-normal", "NORMAL_CONVERSATION"),
                ("Hey TARS analyze the chart", "required-chart", "CHART_ANALYSIS"),
            )
            for transcript, turn_id, expected_intent in required:
                result = utterance(transcript, turn_id)
                assert result["intent"] == expected_intent
                assert result["status"] == "completed"
                assert result["transcript"] == transcript
                assert result["speech_text"]
                assert result["audio_chunks_base64"]
                assert app.state.turn_controller.execution_count(turn_id) == 1

            for index, alias in enumerate(
                ("hey tarz", "hey stars", "tars", "jarvis", "hey jarvis")
            ):
                result = utterance(f"{alias} what time is it", f"alias-{index}")
                assert result["intent"] == "DETERMINISTIC"
                assert result["status"] == "completed"

            stt_before_replay = len(stt.received)
            replay = utterance("Hey TARS explain polymorphism", "required-normal")
            assert replay["replayed"] is True
            assert len(stt.received) == stt_before_replay
            assert app.state.turn_controller.execution_count("required-normal") == 1

            traces = api.get("/api/v1/diagnostics/voice-latency").json()["traces"]
            trace = next(item for item in traces if item["turn_id"] == "required-normal")
            for marker in (
                "audio_detected_at",
                "speech_end_at",
                "stt_started_at",
                "stt_completed_at",
                "wake_detected_at",
                "command_ready_at",
                "processing_started_at",
                "first_response_token_at",
                "tts_started_at",
                "tts_completed_at",
            ):
                assert trace[marker], marker
            assert trace["transcript"] == "Hey TARS explain polymorphism"
            assert trace["wake_match"] == "hey tars"
    finally:
        get_settings.cache_clear()
