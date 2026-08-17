from __future__ import annotations

import time

from voice.audio_utils import pcm16_to_wav


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
