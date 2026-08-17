from __future__ import annotations

import pytest

from app.config import Settings
from voice.errors import VoiceProviderError
from voice.factory import build_stt_provider, build_tts_provider, build_wake_word_provider


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_build_wake_word_provider_mock():
    from voice.providers.mock import MockWakeWordProvider

    provider = build_wake_word_provider(_settings(wake_word_provider="mock"))
    assert isinstance(provider, MockWakeWordProvider)


def test_build_wake_word_provider_openwakeword_without_model_path_raises():
    with pytest.raises(VoiceProviderError):
        build_wake_word_provider(
            _settings(wake_word_provider="openwakeword", wake_word_model_path=None)
        )


def test_build_wake_word_provider_unknown_raises():
    with pytest.raises(ValueError):
        build_wake_word_provider(_settings(wake_word_provider="not_a_real_provider"))


def test_build_stt_provider_mock():
    from voice.providers.mock import MockSpeechToTextProvider

    provider = build_stt_provider(_settings(stt_provider="mock"))
    assert isinstance(provider, MockSpeechToTextProvider)


def test_build_stt_provider_unknown_raises():
    with pytest.raises(ValueError):
        build_stt_provider(_settings(stt_provider="not_a_real_provider"))


def test_build_tts_provider_mock():
    from voice.providers.mock import MockTextToSpeechProvider

    provider = build_tts_provider(_settings(tts_provider="mock"))
    assert isinstance(provider, MockTextToSpeechProvider)


def test_build_tts_provider_fish_speech_requires_api_url():
    with pytest.raises(VoiceProviderError):
        build_tts_provider(_settings(tts_provider="fish_speech", fish_speech_api_url=""))


def test_build_tts_provider_unknown_raises():
    with pytest.raises(ValueError):
        build_tts_provider(_settings(tts_provider="not_a_real_provider"))
