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
    assert body["wake_word_provider"] == "transcript_matcher"
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


def test_manual_listening_endpoints_404_when_gemini_live_not_active(client):
    """conftest.py's client fixture always sets GEMINI_LIVE_ENABLED=false
    (never opens a real mic in tests), so app.state.gemini_live_loop is
    None -- the manual-listening endpoints must say so explicitly (404),
    not silently no-op, so the frontend can distinguish "off" from "not
    available here"."""
    assert client.app.state.gemini_live_loop is None
    assert client.post("/api/v1/voice/manual-listening/start").status_code == 404
    assert client.post("/api/v1/voice/manual-listening/stop").status_code == 404
    assert client.get("/api/v1/voice/gemini-status").json()["enabled"] is False


def test_manual_listening_start_stop_endpoints_drive_the_real_loop(client):
    """Attaches a real (unstarted -- no thread, no PyAudio) GeminiLiveLoop
    to app.state, the same object type app/main.py wires in when Gemini
    Live is actually enabled, and drives it purely through the REST
    endpoints the frontend button calls -- proving the HTTP layer
    correctly reaches GeminiLiveLoop.start_manual_listening()/
    stop_manual_listening() and that gemini-status reflects the change."""
    import asyncio

    from app.config import Settings
    from voice.gemini_live_loop import GeminiLiveLoop

    loop = GeminiLiveLoop(
        settings=Settings(),
        controller=client.app.state.turn_controller,
        event_loop=asyncio.new_event_loop(),
        voice_providers=client.app.state.voice_providers,
    )
    loop._turn_phase = "listening"
    client.app.state.gemini_live_loop = loop

    status_before = client.get("/api/v1/voice/gemini-status").json()
    assert status_before["enabled"] is True
    assert status_before["manual_listening_enabled"] is False

    start_resp = client.post("/api/v1/voice/manual-listening/start")
    assert start_resp.status_code == 200
    assert start_resp.json() == {"manual_listening_enabled": True, "state": "listening"}
    assert loop._manual_listening_enabled is True

    status_on = client.get("/api/v1/voice/gemini-status").json()
    assert status_on["manual_listening_enabled"] is True
    assert status_on["state"] == "listening"

    stop_resp = client.post("/api/v1/voice/manual-listening/stop")
    assert stop_resp.status_code == 200
    assert stop_resp.json() == {"manual_listening_enabled": False, "state": "voice_off"}
    assert loop._manual_listening_enabled is False

    status_off = client.get("/api/v1/voice/gemini-status").json()
    assert status_off["manual_listening_enabled"] is False
    assert status_off["state"] == "voice_off"


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


def test_two_stage_wake_acknowledges_then_executes_next_utterance(client):
    _wait_for_voice_ready(client)
    stt = _WakeSentenceSTT("Hey TARS")
    client.app.state.voice_providers.stt = stt
    wav_bytes = pcm16_to_wav(b"\x01\x00" * 800, sample_rate=16000)
    conversation_id = "5756628d-5b38-4eb2-8640-e2c03912a702"

    wake = client.post(
        "/api/v1/voice/utterance",
        files={"file": ("wake.wav", wav_bytes, "audio/wav")},
        data={
            "turn_id": "wake-only-turn",
            "conversation_id": conversation_id,
            "session_id": "two-stage-test",
        },
    )
    assert wake.status_code == 200
    assert wake.json()["status"] == "awaiting_command"
    assert wake.json()["speech_text"] == "Yeah?"
    assert len(wake.json()["audio_chunks_base64"]) == 1

    stt.text = "Explain Docker containers"
    command = client.post(
        "/api/v1/voice/utterance",
        files={"file": ("command.wav", wav_bytes, "audio/wav")},
        data={
            "turn_id": "two-stage-command-turn",
            "conversation_id": conversation_id,
            "session_id": "two-stage-test",
        },
    )
    assert command.status_code == 200
    body = command.json()
    assert body["intent"] == "NORMAL_CONVERSATION"
    assert body["status"] == "completed"
    assert len(body["audio_chunks_base64"]) == 2
    assert client.app.state.turn_controller.execution_count("wake-only-turn") == 0
    assert client.app.state.turn_controller.execution_count("two-stage-command-turn") == 1

    traces = client.get("/api/v1/diagnostics/voice-latency").json()["traces"]
    command_trace = next(
        item for item in traces if item["turn_id"] == "two-stage-command-turn"
    )
    assert command_trace["wake_match"] == "two_stage_command"
    assert command_trace["wake_detected_at"] is None
    assert command_trace["command_ready_at"]
    assert command_trace["tts_started_at"]
    assert command_trace["tts_completed_at"]


def test_two_stage_command_timeout_is_recorded_at_exact_stage(client):
    _wait_for_voice_ready(client)
    client.app.state.turn_controller._settings.wake_command_timeout_seconds = 0.01
    client.app.state.voice_providers.stt = _WakeSentenceSTT("Hey TARS")
    wav_bytes = pcm16_to_wav(b"\x01\x00" * 400, sample_rate=16000)

    wake = client.post(
        "/api/v1/voice/utterance",
        files={"file": ("wake.wav", wav_bytes, "audio/wav")},
        data={"turn_id": "timeout-turn", "session_id": "timeout-session"},
    )
    assert wake.status_code == 200
    assert wake.json()["status"] == "awaiting_command"
    time.sleep(0.05)

    traces = client.get("/api/v1/diagnostics/voice-latency").json()["traces"]
    trace = next(item for item in traces if item["turn_id"] == "timeout-turn")
    assert trace["error_stage"] == "command_timeout"
    assert trace["status"] == "failed"
