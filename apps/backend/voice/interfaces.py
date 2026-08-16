"""The three voice provider interfaces named in ARCHITECTURE.md § Voice
orchestration (`AssistantProvider` lives in assistant/provider.py — this
module covers the voice-specific three: `WakeWordProvider`,
`SpeechToTextProvider`, `TextToSpeechProvider`). Every interface ships a
mock/local implementation with zero external dependencies, per ADR-005/011,
so the backend runs with no API keys and no downloaded models configured.

Audio convention throughout this package: 16-bit signed PCM, mono, at the
provider's declared `sample_rate` — the same shape openWakeWord,
faster-whisper, and Silero VAD all expect natively, so no adapter needs to
resample except at its own model's required rate.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class WakeWordResult:
    detected: bool
    phrase: str | None = None
    score: float | None = None


class WakeWordProvider(ABC):
    """Streaming wake-word detector. `process_chunk` is called repeatedly
    with successive audio chunks; implementations keep their own rolling
    buffer internally (openWakeWord's Model does this natively)."""

    name: str
    sample_rate: int

    @abstractmethod
    async def process_chunk(self, pcm_audio: bytes) -> WakeWordResult: ...


@dataclass
class TranscriptionResult:
    text: str
    language: str | None = None


class SpeechToTextProvider(ABC):
    name: str
    sample_rate: int

    @abstractmethod
    async def transcribe(self, pcm_audio: bytes) -> TranscriptionResult: ...


@dataclass
class SynthesisResult:
    audio: bytes  # WAV-encoded (RIFF header + PCM data)
    sample_rate: int


class TextToSpeechProvider(ABC):
    name: str

    @abstractmethod
    async def synthesize(self, text: str) -> SynthesisResult: ...
