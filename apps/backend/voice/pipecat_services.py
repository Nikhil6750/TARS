"""Generic Pipecat STT/TTS services that wrap our own `SpeechToTextProvider`
/ `TextToSpeechProvider` implementations, rather than using Pipecat's
vendor-specific service classes directly. This keeps exactly one code path
per provider — the same `FasterWhisperSTTProvider` instance (say) answers
both the realtime voice pipeline and the single-shot REST transcribe
endpoint (Phase E) — instead of configuring faster-whisper twice with two
different integrations that could drift apart.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncGenerator

from pipecat.frames.frames import ErrorFrame, Frame, TranscriptionFrame, TTSAudioRawFrame
from pipecat.services.stt_service import SegmentedSTTService
from pipecat.services.tts_service import TTSService
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601

from voice.audio_utils import wav_to_pcm16
from voice.errors import VoiceProviderError
from voice.interfaces import SpeechToTextProvider, TextToSpeechProvider

logger = logging.getLogger("tars.voice.pipecat_services")


class ProviderBridgeSTTService(SegmentedSTTService):
    """Runs a complete VAD-segmented utterance through our
    `SpeechToTextProvider.transcribe`. Segmentation/buffering is handled
    entirely by `SegmentedSTTService` — this class only implements
    `run_stt`."""

    def __init__(self, provider: SpeechToTextProvider, **kwargs):
        super().__init__(sample_rate=provider.sample_rate, **kwargs)
        self._provider = provider

    @property
    def wants_wav_segments(self) -> bool:
        # Our providers read raw PCM16 directly (see
        # FasterWhisperSTTProvider._transcribe_sync), not a WAV container.
        return False

    # Pipecat's own abstract declaration (`async def run_stt(...) ->
    # AsyncGenerator[...]`) types the method itself as a coroutine
    # returning a generator rather than as an async-generator function, so
    # mypy flags any real `async def ... yield` implementation — including
    # Pipecat's own built-in services — as an override mismatch. Runtime
    # behavior is correct: SegmentedSTTService calls this with
    # `process_generator(self.run_stt(audio))`, i.e. `async for`, not `await`.
    async def run_stt(  # type: ignore[override]
        self, audio: bytes
    ) -> AsyncGenerator[Frame | None, None]:
        try:
            result = await self._provider.transcribe(audio)
        except VoiceProviderError as exc:
            logger.error("STT provider failed: %s", exc)
            yield ErrorFrame(error=str(exc))
            return
        if result.text:
            try:
                language = Language(result.language) if result.language else None
            except ValueError:
                language = None
            yield TranscriptionFrame(
                text=result.text,
                user_id="",
                timestamp=time_now_iso8601(),
                language=language,
            )


class ProviderBridgeTTSService(TTSService):
    """Runs each TTS turn through our `TextToSpeechProvider.synthesize`.
    Non-streaming by design — our providers return a complete WAV clip, so
    this yields exactly one audio frame per turn rather than incremental
    chunks."""

    def __init__(self, provider: TextToSpeechProvider, sample_rate: int, **kwargs):
        super().__init__(
            sample_rate=sample_rate, push_start_frame=True, push_stop_frames=True, **kwargs
        )
        self._provider = provider

    # See run_stt's comment above — same mypy false positive on abstract
    # async-generator method overrides.
    async def run_tts(  # type: ignore[override]
        self, text: str, context_id: str
    ) -> AsyncGenerator[Frame | None, None]:
        try:
            await self.start_ttfb_metrics()
            result = await self._provider.synthesize(text)
            pcm, sample_rate = wav_to_pcm16(result.audio)
        except VoiceProviderError as exc:
            logger.error("TTS provider failed: %s", exc)
            yield ErrorFrame(error=str(exc))
            return
        finally:
            await self.stop_ttfb_metrics()

        yield TTSAudioRawFrame(
            audio=pcm, sample_rate=sample_rate, num_channels=1, context_id=context_id
        )
