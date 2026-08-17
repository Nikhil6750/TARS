"""Zero-dependency mock implementations of all three voice provider
interfaces — the guaranteed fallback so the backend's voice endpoints work
with no models, no packages, and no hardware, per ADR-005/011."""
from __future__ import annotations

from voice.audio_utils import pcm16_to_wav
from voice.interfaces import (
    SpeechToTextProvider,
    SynthesisResult,
    TextToSpeechProvider,
    TranscriptionResult,
    WakeWordProvider,
    WakeWordResult,
)


class MockWakeWordProvider(WakeWordProvider):
    name = "mock"
    sample_rate = 16000

    async def process_chunk(self, pcm_audio: bytes) -> WakeWordResult:
        return WakeWordResult(detected=False)


class MockSpeechToTextProvider(SpeechToTextProvider):
    name = "mock"
    sample_rate = 16000

    async def transcribe(self, pcm_audio: bytes) -> TranscriptionResult:
        return TranscriptionResult(text="", language=None)


class MockTextToSpeechProvider(TextToSpeechProvider):
    name = "mock"
    sample_rate = 16000

    async def synthesize(self, text: str) -> SynthesisResult:
        # A single silent frame — a valid, playable WAV with no audio
        # content. Proves the pipeline's audio-out path end to end without
        # needing a real voice.
        silence = b"\x00\x00" * self.sample_rate  # 1 second of silence
        return SynthesisResult(audio=pcm16_to_wav(silence, self.sample_rate), sample_rate=self.sample_rate)
