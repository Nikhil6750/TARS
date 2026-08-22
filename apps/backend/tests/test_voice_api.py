from __future__ import annotations

import time

from voice.audio_utils import pcm16_to_wav
from voice.interfaces import SpeechToTextProvider, TranscriptionResult


class _WakeSentenceSTT(SpeechToTextProvider):
    name = "test_stt"
    sample_rate = 16000

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    async def transcribe(self, pcm_audio: bytes) -> TranscriptionResult:
        self.calls += 1
        return TranscriptionResult(text=self.text, language="en")


def _wait_for_voice_ready(client, attempts: int = 40) -> dict:
    body = {}
    for _ in range(attempts):
        resp = client.get("/api/v1/voice/status")
        body = resp.json()
        if body["ready"]:
            return body
        time.sleep(0.05)
    return body


def test_voice_status_reports_configured_mock_providers(client):
    body = _wait_for_voice_ready(client)
    assert body["ready"] is True
    assert body["wake_word_provider"] == "mock"
    assert body["stt_provider"] == "mock"
    assert body["tts_provider"] == "mock"


def test_synthesize_returns_valid_wav(client):
    _wait_for_voice_ready(client)
    resp = client.post("/api/v1/voice/synthesize", json={"text": "hello there"})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/wav"
    assert resp.content[:4] == b"RIFF"


def test_transcribe_accepts_wav_upload(client):
    _wait_for_voice_ready(client)
    wav_bytes = pcm16_to_wav(b"\x00\x00" * 1600, sample_rate=16000)
    resp = client.post(
        "/api/v1/voice/transcribe",
        files={"file": ("clip.wav", wav_bytes, "audio/wav")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["text"] == ""


def test_transcribe_rejects_invalid_audio(client):
    _wait_for_voice_ready(client)
    resp = client.post(
        "/api/v1/voice/transcribe",
        files={"file": ("clip.wav", b"not a wav file", "audio/wav")},
    )
    assert resp.status_code == 422


def test_canonical_utterance_runs_stt_wake_and_one_assistant_turn(client):
    _wait_for_voice_ready(client)
    stt = _WakeSentenceSTT("Hey TARS, explain polymorphism")
    client.app.state.voice_providers.stt = stt
    wav_bytes = pcm16_to_wav(b"\x01\x00" * 1600, sample_rate=16000)

    response = client.post(
        "/api/v1/voice/utterance",
        files={"file": ("utterance.wav", wav_bytes, "audio/wav")},
        data={
            "turn_id": "physical-turn-1",
            "conversation_id": "e57b1ba8-6c31-4d52-9f69-9d5897a51d8b",
            "session_id": "native-test",
            "audio_detected_at_ms": "1787382000000",
            "speech_end_at_ms": "1787382000500",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["turn_id"] == "physical-turn-1"
    assert body["intent"] == "NORMAL_CONVERSATION"
    assert body["status"] == "completed"
    assert body["speech_text"]
    assert stt.calls == 1
    assert client.app.state.turn_controller.execution_count("physical-turn-1") == 1

    replay = client.post(
        "/api/v1/voice/utterance",
        files={"file": ("utterance.wav", wav_bytes, "audio/wav")},
        data={
            "turn_id": "physical-turn-1",
            "conversation_id": "e57b1ba8-6c31-4d52-9f69-9d5897a51d8b",
            "session_id": "native-test",
        },
    )
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert stt.calls == 1


def test_canonical_utterance_records_wake_match_failure_stage(client):
    _wait_for_voice_ready(client)
    stt = _WakeSentenceSTT("ordinary room noise")
    client.app.state.voice_providers.stt = stt
    wav_bytes = pcm16_to_wav(b"\x01\x00" * 800, sample_rate=16000)

    response = client.post(
        "/api/v1/voice/utterance",
        files={"file": ("ambient.wav", wav_bytes, "audio/wav")},
        data={"turn_id": "no-wake-turn", "session_id": "native-test"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    traces = client.get("/api/v1/diagnostics/voice-latency").json()["traces"]
    trace = next(item for item in traces if item["turn_id"] == "no-wake-turn")
    assert trace["transcript"] == "ordinary room noise"
    assert trace["wake_match"] is None
    assert trace["error_stage"] == "wake_match"
