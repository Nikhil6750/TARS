"""Builds the configured wake-word/STT/TTS providers from Settings — the
single place that knows every concrete voice adapter, mirroring
assistant/factory.py's pattern for AssistantProvider."""
from __future__ import annotations

from app.config import Settings
from voice.interfaces import SpeechToTextProvider, TextToSpeechProvider, WakeWordProvider
from voice.providers.mock import (
    MockSpeechToTextProvider,
    MockTextToSpeechProvider,
    MockWakeWordProvider,
)


def build_wake_word_provider(settings: Settings) -> WakeWordProvider:
    provider = settings.wake_word_provider.lower()
    if provider == "mock":
        return MockWakeWordProvider()
    if provider == "openwakeword":
        from voice.providers.openwakeword_provider import OpenWakeWordProvider

        return OpenWakeWordProvider(
            model_path=settings.wake_word_model_path or "",
            phrase=settings.wake_word_phrase,
            threshold=settings.wake_word_threshold,
        )
    raise ValueError(f"Unknown WAKE_WORD_PROVIDER '{settings.wake_word_provider}'")


def build_stt_provider(settings: Settings) -> SpeechToTextProvider:
    provider = settings.stt_provider.lower()
    if provider == "mock":
        return MockSpeechToTextProvider()
    if provider == "faster_whisper":
        from voice.providers.faster_whisper_stt import FasterWhisperSTTProvider

        return FasterWhisperSTTProvider(
            model_size=settings.faster_whisper_model,
            device=settings.faster_whisper_device,
            compute_type=settings.faster_whisper_compute_type,
        )
    raise ValueError(f"Unknown STT_PROVIDER '{settings.stt_provider}'")


def build_tts_provider(settings: Settings) -> TextToSpeechProvider:
    provider = settings.tts_provider.lower()
    if provider == "mock":
        return MockTextToSpeechProvider()
    if provider == "kokoro":
        from voice.providers.kokoro_tts import KokoroTTSProvider

        return KokoroTTSProvider(
            voice=settings.kokoro_voice,
            lang=settings.kokoro_lang,
            model_path=settings.kokoro_model_path,
            voices_path=settings.kokoro_voices_path,
        )
    if provider == "fish_speech":
        from voice.providers.fish_speech_tts import FishSpeechTTSProvider

        return FishSpeechTTSProvider(
            api_url=settings.fish_speech_api_url,
            reference_id=settings.fish_speech_reference_id,
        )
    raise ValueError(f"Unknown TTS_PROVIDER '{settings.tts_provider}'")
