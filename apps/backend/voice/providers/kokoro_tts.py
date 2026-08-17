"""Local TTS via kokoro-onnx — the lightweight fallback TextToSpeechProvider
per ADR-011. Fully local/offline inference (no API key); model + voices
files are downloaded once from GitHub releases on first use unless
`model_path`/`voices_path` point at already-downloaded local files, then
cached under `~/.cache/pipecat/kokoro-onnx` (shared with Pipecat's own
KokoroTTSService, used by the realtime pipeline — see voice/pipeline.py —
so the model is only ever fetched once regardless of which path runs).
"""
from __future__ import annotations

import asyncio

from voice.audio_utils import float32_to_pcm16, pcm16_to_wav
from voice.errors import VoiceProviderError
from voice.interfaces import SynthesisResult, TextToSpeechProvider
from voice.kokoro_models import resolve_kokoro_paths


class KokoroTTSProvider(TextToSpeechProvider):
    name = "kokoro"

    def __init__(
        self,
        voice: str = "af_heart",
        lang: str = "en-us",
        model_path: str | None = None,
        voices_path: str | None = None,
    ):
        try:
            from kokoro_onnx import Kokoro
        except ImportError as exc:
            raise VoiceProviderError(
                "kokoro-onnx is not installed — pip install -r requirements-voice.txt"
            ) from exc

        self._voice = voice
        self._lang = lang
        resolved_model, resolved_voices = resolve_kokoro_paths(model_path, voices_path)
        try:
            self._kokoro = Kokoro(resolved_model, resolved_voices)
        except Exception as exc:
            raise VoiceProviderError(f"failed to load Kokoro model: {exc}") from exc

    async def synthesize(self, text: str) -> SynthesisResult:
        return await asyncio.to_thread(self._synthesize_sync, text)

    def _synthesize_sync(self, text: str) -> SynthesisResult:
        try:
            samples, sample_rate = self._kokoro.create(text, voice=self._voice, lang=self._lang)
        except Exception as exc:
            raise VoiceProviderError(f"Kokoro synthesis failed: {exc}") from exc
        pcm = float32_to_pcm16(samples)
        return SynthesisResult(audio=pcm16_to_wav(pcm, sample_rate), sample_rate=sample_rate)
