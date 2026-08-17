"""Local speech-to-text via faster-whisper (CTranslate2-based Whisper
inference) — the default/required SpeechToTextProvider per ADR-011. Model
weights download from Hugging Face on first use per `model_size` and are
cached locally afterward (`~/.cache/huggingface`); no API key, no network
required after that first pull.
"""
from __future__ import annotations

import asyncio

from voice.errors import VoiceProviderError
from voice.interfaces import SpeechToTextProvider, TranscriptionResult

PCM16_SCALE = 32768.0


class FasterWhisperSTTProvider(SpeechToTextProvider):
    name = "faster_whisper"
    sample_rate = 16000

    def __init__(self, model_size: str = "base", device: str = "cpu", compute_type: str = "int8"):
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise VoiceProviderError(
                "faster-whisper is not installed — pip install -r requirements-voice.txt"
            ) from exc

        try:
            self._model = WhisperModel(model_size, device=device, compute_type=compute_type)
        except Exception as exc:
            raise VoiceProviderError(
                f"failed to load faster-whisper model '{model_size}': {exc}"
            ) from exc

    async def transcribe(self, pcm_audio: bytes) -> TranscriptionResult:
        return await asyncio.to_thread(self._transcribe_sync, pcm_audio)

    def _transcribe_sync(self, pcm_audio: bytes) -> TranscriptionResult:
        import numpy as np

        samples = np.frombuffer(pcm_audio, dtype=np.int16).astype(np.float32) / PCM16_SCALE
        try:
            segments, info = self._model.transcribe(samples, language=None)
            text = " ".join(segment.text.strip() for segment in segments).strip()
        except Exception as exc:
            raise VoiceProviderError(f"faster-whisper transcription failed: {exc}") from exc
        return TranscriptionResult(text=text, language=info.language if info else None)
