"""Exercises the real local voice adapters against actual model weights.

These are the only tests in the suite that touch real ML models rather
than mocks — they're expected to work in this dev environment (models are
already cached; see the Phase D handoff for first-run download timings),
but a from-scratch CI runner with no network would have nothing to
download from, so failures here are skipped rather than failing the suite
— consistent with Codex's zero-API-key acceptance path never depending on
these optional local models being present.
"""
from __future__ import annotations

import pytest

pytest.importorskip("faster_whisper")
pytest.importorskip("kokoro_onnx")


async def test_faster_whisper_transcribes_silence_without_crashing():
    from voice.errors import VoiceProviderError
    from voice.providers.faster_whisper_stt import FasterWhisperSTTProvider

    try:
        provider = FasterWhisperSTTProvider(model_size="tiny", device="cpu", compute_type="int8")
    except VoiceProviderError as exc:
        pytest.skip(f"faster-whisper model unavailable in this environment: {exc}")

    silence = b"\x00\x00" * 16000  # 1 second of 16kHz PCM16 silence
    result = await provider.transcribe(silence)
    assert result.text == ""


async def test_kokoro_synthesizes_valid_wav():
    from voice.errors import VoiceProviderError
    from voice.providers.kokoro_tts import KokoroTTSProvider

    try:
        provider = KokoroTTSProvider()
    except VoiceProviderError as exc:
        pytest.skip(f"Kokoro model unavailable in this environment: {exc}")

    result = await provider.synthesize("Testing one two three.")
    assert result.audio[:4] == b"RIFF"
    assert result.sample_rate > 0
    assert len(result.audio) > 44  # more than just a WAV header
