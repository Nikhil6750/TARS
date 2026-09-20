"""Voice provider lifecycle for the FastAPI app. STT/TTS local model
construction can take anywhere from ~2s (warm, cached) to several minutes
(cold, first-run download — see the Phase D handoff for measured numbers),
so it runs in a background task rather than blocking app startup: the
event/health/assistant endpoints become available immediately, and voice
endpoints report "loading" via `VoiceProviders.ready` until construction
finishes (or falls back to mock on failure, same defensive pattern as
assistant_provider in app.main).
"""
from __future__ import annotations

import asyncio
import logging

from app.config import Settings
from voice.factory import build_stt_provider, build_tts_provider
from voice.interfaces import SpeechToTextProvider, TextToSpeechProvider, WakeWordProvider
from voice.providers.mock import (
    MockSpeechToTextProvider,
    MockTextToSpeechProvider,
    MockWakeWordProvider,
)

logger = logging.getLogger("tars.voice_state")


class VoiceProviders:
    def __init__(self) -> None:
        self.wake_word: WakeWordProvider = MockWakeWordProvider()
        self.stt: SpeechToTextProvider = MockSpeechToTextProvider()
        self.tts: TextToSpeechProvider = MockTextToSpeechProvider()
        self.ready = asyncio.Event()
        self.load_error: str | None = None

    async def load(self, settings: Settings) -> None:
        loop = asyncio.get_running_loop()
        try:
            # Production wake recognition is transcript matching in the turn
            # controller. Keep the provider interface dormant; do not load or
            # claim an openWakeWord model that does not exist.
            self.wake_word = MockWakeWordProvider()
        except Exception:
            logger.exception(
                "failed to construct wake word provider '%s' — falling back to mock",
                settings.wake_word_provider,
            )

        try:
            self.stt = await loop.run_in_executor(None, build_stt_provider, settings)
        except Exception:
            logger.exception(
                "failed to construct STT provider '%s' — falling back to mock",
                settings.stt_provider,
            )
            self.load_error = "STT provider failed to load"

        try:
            self.tts = await loop.run_in_executor(None, build_tts_provider, settings)
        except Exception:
            logger.exception(
                "failed to construct TTS provider '%s' — falling back to mock",
                settings.tts_provider,
            )
            self.load_error = "TTS provider failed to load"
            if settings.tts_provider == "pocket":
                try:
                    self.tts = await loop.run_in_executor(
                        None, build_tts_provider, settings.model_copy(update={"tts_provider": "kokoro"})
                    )
                    self.load_error = "Pocket unavailable; using local Kokoro fallback"
                except Exception:
                    logger.exception("local Kokoro fallback also unavailable")

        logger.info(
            "voice providers ready: wake=transcript_matcher stt=%s tts=%s",
            self.stt.name,
            self.tts.name,
        )
        self.ready.set()
