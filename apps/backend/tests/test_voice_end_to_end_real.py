"""Proves the certified real local voice path end to end:

    recorded audio bytes -> faster-whisper STT -> non-hardcoded
    transcription -> AssistantRouter (over the real HTTP surface) ->
    assistant response -> local Kokoro TTS -> playable audio bytes

No step is faked or bypassed: the "recorded audio" fixture is itself real
synthesized speech (Kokoro), independently decoded by a different model
(faster-whisper), and the transcribed text — not the original hardcoded
phrase — is what gets sent to the assistant. A canned/bypassed STT stage
would fail the "distinct audio -> distinct text" assertion below because a
fixed substitution returns the same text regardless of input audio.

Skipped (not failed) when faster-whisper/kokoro-onnx or their model
weights are unavailable in this environment — consistent with
test_voice_real_adapters.py and the zero-paid-key requirement that the
acceptance path never depends on optional local models being present. See
docs/coordination/handoffs/claude.md for what was and was not actually
executed in a given environment.
"""
from __future__ import annotations

import asyncio

import numpy as np
import pytest
from scipy.signal import resample_poly

from voice.audio_utils import wav_to_pcm16
from voice.errors import VoiceProviderError

pytest.importorskip("faster_whisper")
pytest.importorskip("kokoro_onnx")

ATTENTION_PHRASE = "What setups require my attention?"
CONTROL_PHRASE = "Turn off all the lights in the kitchen."


def _normalize(text: str) -> str:
    return text.strip().rstrip(".?!").lower()


def _resample_to_16k(wav_bytes: bytes) -> bytes:
    pcm, sample_rate = wav_to_pcm16(wav_bytes)
    samples = np.frombuffer(pcm, dtype=np.int16)
    if sample_rate != 16000:
        samples = resample_poly(samples, 16000, sample_rate).astype(np.int16)
    return samples.tobytes()


@pytest.fixture(scope="module")
def real_stt():
    from voice.providers.faster_whisper_stt import FasterWhisperSTTProvider

    try:
        return FasterWhisperSTTProvider(model_size="base", device="cpu", compute_type="int8")
    except VoiceProviderError as exc:
        pytest.skip(f"faster-whisper model unavailable in this environment: {exc}")


@pytest.fixture(scope="module")
def real_tts():
    from voice.providers.kokoro_tts import KokoroTTSProvider

    try:
        return KokoroTTSProvider()
    except VoiceProviderError as exc:
        pytest.skip(f"Kokoro model unavailable in this environment: {exc}")


def test_real_stt_transcribes_distinct_audio_to_distinct_non_hardcoded_text(real_stt, real_tts):
    """Guards against a bypassed/faked STT stage: two different spoken
    phrases must decode to two different transcriptions. A hardcoded/canned
    substitution would return identical (or empty) text regardless of the
    input audio."""
    attention_audio = asyncio.run(real_tts.synthesize(ATTENTION_PHRASE)).audio
    control_audio = asyncio.run(real_tts.synthesize(CONTROL_PHRASE)).audio

    attention_text = asyncio.run(real_stt.transcribe(_resample_to_16k(attention_audio))).text
    control_text = asyncio.run(real_stt.transcribe(_resample_to_16k(control_audio))).text

    assert attention_text.strip() != ""
    assert control_text.strip() != ""
    assert attention_text != control_text
    assert "attention" in attention_text.lower()
    assert "attention" not in control_text.lower()


def test_real_voice_round_trip_reaches_assistant_router_and_local_tts(client, real_stt, real_tts):
    """Full certified path: recorded audio -> faster-whisper -> real
    transcription -> AssistantRouter (real HTTP endpoint) -> assistant
    response -> local TTS audio bytes."""
    spoken_audio = asyncio.run(real_tts.synthesize(ATTENTION_PHRASE)).audio
    transcription = asyncio.run(real_stt.transcribe(_resample_to_16k(spoken_audio))).text

    assert transcription.strip() != ""
    # The transcription must actually reflect the spoken audio, not a fixed
    # placeholder — fail loudly if STT drifted or was substituted.
    assert _normalize(transcription) == _normalize(ATTENTION_PHRASE)

    resp = client.post("/api/v1/assistant/query", json={"text": transcription})
    assert resp.status_code == 200
    body = resp.json()

    # Deterministic routing proves AssistantRouter matched on the real
    # transcribed text (never a hardcoded intent unrelated to the audio).
    assert body["intent"] == "DETERMINISTIC"
    assert body["provider"] == "deterministic"
    assistant_text = body["speech_text"]
    assert assistant_text.strip() != ""

    spoken_reply = asyncio.run(real_tts.synthesize(assistant_text))
    assert spoken_reply.audio[:4] == b"RIFF"
    assert spoken_reply.sample_rate > 0
    assert len(spoken_reply.audio) > 44  # more than just a WAV header
