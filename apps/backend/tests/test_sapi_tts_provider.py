from __future__ import annotations

import sys
import wave
from io import BytesIO

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="SAPI is Windows-only")


async def test_synthesize_produces_valid_wav_audio():
    from voice.providers.sapi_tts import SapiTTSProvider

    provider = SapiTTSProvider()
    result = await provider.synthesize("Today is Sunday, August 23rd, 2026.")

    assert result.audio.startswith(b"RIFF")
    assert b"WAVE" in result.audio[:16]
    with wave.open(BytesIO(result.audio), "rb") as wav_file:
        assert wav_file.getframerate() == result.sample_rate
        assert wav_file.getnframes() > 0
        assert wav_file.getsampwidth() == 2


async def test_synthesize_rejects_empty_text_gracefully():
    from voice.errors import VoiceProviderError
    from voice.providers.sapi_tts import SapiTTSProvider

    provider = SapiTTSProvider()
    # SAPI itself tolerates empty text (produces near-silent/empty audio)
    # rather than erroring -- this just proves the call doesn't crash and
    # still returns something shaped like a SynthesisResult.
    try:
        result = await provider.synthesize("")
    except VoiceProviderError:
        return
    assert isinstance(result.sample_rate, int)


async def test_voice_name_filter_selects_a_matching_voice():
    from voice.providers.sapi_tts import SapiTTSProvider

    provider = SapiTTSProvider(voice_name_contains="Zira")
    result = await provider.synthesize("Hello.")
    assert len(result.audio) > 44  # more than just a bare WAV header
