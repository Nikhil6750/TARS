from __future__ import annotations


async def test_mock_wake_word_never_detects():
    from voice.providers.mock import MockWakeWordProvider

    provider = MockWakeWordProvider()
    result = await provider.process_chunk(b"\x00\x00" * 100)
    assert result.detected is False


async def test_mock_stt_returns_empty_transcript():
    from voice.providers.mock import MockSpeechToTextProvider

    provider = MockSpeechToTextProvider()
    result = await provider.transcribe(b"\x00\x00" * 100)
    assert result.text == ""


async def test_mock_tts_returns_valid_wav():
    from voice.providers.mock import MockTextToSpeechProvider

    provider = MockTextToSpeechProvider()
    result = await provider.synthesize("hello")
    assert result.audio[:4] == b"RIFF"
    assert result.audio[8:12] == b"WAVE"
    assert result.sample_rate == 16000
