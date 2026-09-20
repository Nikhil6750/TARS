from __future__ import annotations

import time
from datetime import datetime

import aiosqlite
import pytest

from app.voice_telemetry import VOICE_STAGES, VoiceTraceStore, VoiceTurnRecorder
from storage.migrator import run_migrations
from voice.audio_utils import pcm16_to_wav
from voice.interfaces import SynthesisResult, TextToSpeechProvider


@pytest.fixture
async def voice_store(tmp_path):
    path = tmp_path / "voice.db"
    run_migrations(path)
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    yield VoiceTraceStore(conn)
    await conn.close()


async def test_voice_turn_recorder_persists_all_required_stage_timestamps(voice_store):
    recorder = VoiceTurnRecorder(voice_store, "conversation-1")
    for stage in VOICE_STAGES:
        await recorder.mark(stage)

    trace = (await voice_store.recent())[0]
    timestamps = [trace[f"{stage}_at"] for stage in VOICE_STAGES]
    assert all(timestamps)
    parsed = [datetime.fromisoformat(value) for value in timestamps]
    assert parsed == sorted(parsed)
    assert trace["status"] == "completed"


def test_rest_transcription_records_audio_and_stt_timestamps(client):
    wav = pcm16_to_wav(b"\x00\x00" * 1600, 16000)
    response = client.post(
        "/api/v1/voice/transcribe",
        files={"file": ("audio.wav", wav, "audio/wav")},
    )

    assert response.status_code == 200
    turn_id = response.json()["telemetry_id"]
    traces = client.get("/api/v1/diagnostics/voice-latency").json()["traces"]
    trace = next(item for item in traces if item["turn_id"] == turn_id)
    assert trace["audio_received_at"]
    assert trace["stt_started_at"]
    assert trace["stt_completed_at"]


class _RecordingTTS(TextToSpeechProvider):
    name = "recording"

    def __init__(self) -> None:
        self.text = ""

    async def synthesize(self, text: str) -> SynthesisResult:
        self.text = text
        return SynthesisResult(
            audio=pcm16_to_wav(b"\x00\x00" * 20, 24000),
            sample_rate=24000,
        )


def test_synthesis_sanitizes_speech_and_records_timestamps(client):
    for _ in range(40):
        if client.get("/api/v1/voice/status").json()["ready"]:
            break
        time.sleep(0.05)
    provider = _RecordingTTS()
    client.app.state.voice_providers.tts = provider
    response = client.post(
        "/api/v1/voice/synthesize",
        json={
            "text": "### Result\n- **Open** `C:\\TARS\\repo` at https://example.com/docs",
        },
    )

    assert response.status_code == 200
    assert provider.text
    for marker in ("###", "**", "*", "`", "https://", "C:\\"):
        assert marker not in provider.text
    turn_id = response.headers["X-TARS-Voice-Turn-ID"]
    traces = client.get("/api/v1/diagnostics/voice-latency").json()["traces"]
    trace = next(item for item in traces if item["turn_id"] == turn_id)
    assert trace["tts_synthesis_started_at"]
    assert trace["tts_ready_at"]
    assert trace["status"] == "completed"
