from __future__ import annotations

import asyncio

from app.config import Settings
from app.voice_state import VoiceProviders
from assistant.turn_controller import WakePhraseMatcher
from voice.errors import VoiceProviderError
from voice.gemini_live_loop import (
    GeminiLiveLoop,
    _downmix_to_pcm16,
    _looks_like_echo,
    _pending_speakable_chunks,
    _resolve_command,
)
from voice.interfaces import SynthesisResult, TextToSpeechProvider

_WAKE_MATCHER = WakePhraseMatcher(Settings().wake_alias_list)


def test_resolve_command_drops_ambient_speech_with_no_wake_phrase_and_no_window():
    """Regression test for the primary bug this pass fixes: ambient room
    speech (TV, another conversation) with no wake phrase and no open
    follow-up window must never become a TARS command."""
    for ambient in (" abhi to nahin hai", "¿Dónde hay peñas?"):
        match = _WAKE_MATCHER.match(ambient)
        assert _resolve_command(ambient, match, follow_up_active=False) is None


def test_resolve_command_executes_a_wake_triggered_utterance():
    text = "Hey TARS, introduce yourself"
    match = _WAKE_MATCHER.match(text)
    assert _resolve_command(text, match, follow_up_active=False) == "introduce yourself"


def test_resolve_command_accepts_a_follow_up_without_wake_phrase_only_when_window_open():
    text = "Explain that more simply"
    match = _WAKE_MATCHER.match(text)
    assert match is None  # no wake phrase in this utterance at all

    assert _resolve_command(text, match, follow_up_active=True) == text
    assert _resolve_command(text, match, follow_up_active=False) is None


def test_resolve_command_wake_phrase_still_works_during_an_open_follow_up_window():
    text = "Hey TARS, what about tomorrow"
    match = _WAKE_MATCHER.match(text)
    assert _resolve_command(text, match, follow_up_active=True) == "what about tomorrow"


def _new_loop() -> GeminiLiveLoop:
    return GeminiLiveLoop(
        settings=Settings(),
        controller=object(),
        event_loop=asyncio.get_running_loop(),
        voice_providers=VoiceProviders(),
    )


async def test_manual_listening_defaults_to_off_on_construction():
    loop = _new_loop()
    assert loop._manual_listening_enabled is False
    assert loop.status.manual_listening_enabled is False


async def test_idle_status_label_reflects_manual_listening_flag():
    loop = _new_loop()
    assert loop._idle_status_label() == "voice_off"
    loop._manual_listening_enabled = True
    assert loop._idle_status_label() == "listening"


async def test_start_manual_listening_enables_and_flushes_stale_mic_audio():
    from voice.gemini_live_loop import _MicCaptureThread

    loop = _new_loop()
    loop._turn_phase = "listening"
    mic_capture = _MicCaptureThread(
        device_index=0, sample_rate=16000, event_loop=asyncio.get_running_loop()
    )
    mic_capture.chunks.put_nowait(b"stale-audio-from-before-start")
    loop._mic_capture = mic_capture
    loop._pending_text = "leftover fragment"

    await loop.start_manual_listening()

    assert loop._manual_listening_enabled is True
    assert loop.status.manual_listening_enabled is True
    assert loop.status.state == "listening"
    assert loop._pending_text == ""
    assert mic_capture.chunks.empty()


async def test_stop_manual_listening_disables_and_clears_turn_local_state():
    from voice.gemini_live_loop import _MicCaptureThread

    loop = _new_loop()
    loop._turn_phase = "listening"
    loop._manual_listening_enabled = True
    loop.status.manual_listening_enabled = True
    loop._pending_text = "partial transcript in flight"
    loop._follow_up_window_until = 99999999999.0
    mic_capture = _MicCaptureThread(
        device_index=0, sample_rate=16000, event_loop=asyncio.get_running_loop()
    )
    mic_capture.chunks.put_nowait(b"queued-frame")
    loop._mic_capture = mic_capture

    await loop.stop_manual_listening()

    assert loop._manual_listening_enabled is False
    assert loop.status.manual_listening_enabled is False
    assert loop.status.state == "voice_off"
    assert loop._pending_text == ""
    assert loop._follow_up_window_until is None
    assert mic_capture.chunks.empty()


async def test_stop_manual_listening_during_an_active_turn_does_not_force_listening_state():
    """If Stop is pressed while TARS is mid-turn (turn_phase not
    "listening"), the idle state must not be overwritten here -- the turn
    finishes on its own and _finish_turn (which reads _idle_status_label()
    fresh) is what decides Voice Off vs Listening afterward."""
    loop = _new_loop()
    loop._turn_phase = "speaking"
    loop._manual_listening_enabled = True
    loop.status.state = "speaking"

    await loop.stop_manual_listening()

    assert loop._manual_listening_enabled is False
    assert loop.status.state == "speaking"  # untouched -- turn still in flight


def test_looks_like_echo_matches_exact_normalized_response():
    assert _looks_like_echo(
        "today is Sunday, August 23rd, 2026",
        "Today is Sunday, August 23rd, 2026.",
    )


def test_looks_like_echo_matches_substantial_overlap():
    assert _looks_like_echo(
        "docker containers are lightweight isolated",
        "Docker containers are lightweight, isolated runtime environments "
        "that package an application with its dependencies.",
    )


def test_looks_like_echo_does_not_flag_unrelated_speech():
    assert not _looks_like_echo("open tradingview", "Today is Sunday, August 23rd, 2026.")


def test_looks_like_echo_handles_no_prior_response():
    assert not _looks_like_echo("open tradingview", None)
    assert not _looks_like_echo("open tradingview", "")


def test_looks_like_echo_ignores_trivially_short_overlap():
    # A short shared word ("the") should not itself trigger echo detection.
    assert not _looks_like_echo("the", "What is the time")


def test_downmix_to_pcm16_produces_half_the_frame_count():
    import numpy as np

    stereo = np.zeros(200, dtype="<f4")  # 100 stereo frames
    pcm16 = _downmix_to_pcm16(stereo.tobytes())
    assert len(pcm16) == 100 * 2  # 100 mono int16 samples


async def test_gemini_live_loop_constructs_with_ears_only_initial_state():
    """Construction alone (no real Gemini connection) must leave the loop
    in a safe, known state: not mid-turn, not playing anything, and
    holding the live (mutable-in-place) VoiceProviders reference rather
    than a snapshot -- see GeminiLiveLoop.__init__'s docstring on why a
    snapshot would silently keep using the mock TTS provider forever."""
    voice_providers = VoiceProviders()
    loop = GeminiLiveLoop(
        settings=Settings(),
        controller=object(),
        event_loop=asyncio.get_running_loop(),
        voice_providers=voice_providers,
    )

    assert loop._turn_phase == "listening"
    assert loop._playback_active is False
    assert loop._active_turn_id is None
    assert loop.status.state == "stopped"
    assert loop._voice_providers is voice_providers

    voice_providers.tts = object()  # simulate VoiceProviders.load() swapping it in place
    assert loop._voice_providers.tts is voice_providers.tts


def test_pending_speakable_chunks_withholds_incomplete_trailing_fragment():
    # "Docker containers are lightweight" has no terminal punctuation yet
    # and is short -- more text is presumably still streaming in, so it
    # should not be spoken yet.
    chunks = ["Docker containers are lightweight."]
    all_chunks = ["Docker containers are lightweight.", "They are isolated"]
    assert _pending_speakable_chunks(chunks, 0, is_final=False) == [
        "Docker containers are lightweight."
    ]
    assert _pending_speakable_chunks(all_chunks, 0, is_final=False) == [
        "Docker containers are lightweight."
    ]


def test_pending_speakable_chunks_speaks_everything_when_final():
    all_chunks = ["Docker containers are lightweight.", "They are isolated"]
    assert _pending_speakable_chunks(all_chunks, 0, is_final=True) == all_chunks


def test_pending_speakable_chunks_only_returns_new_chunks_beyond_already_spoken():
    all_chunks = ["First sentence.", "Second sentence.", "Third sentence."]
    assert _pending_speakable_chunks(all_chunks, 2, is_final=True) == ["Third sentence."]
    assert _pending_speakable_chunks(all_chunks, 3, is_final=True) == []


def test_pending_speakable_chunks_forces_a_chunk_after_enough_words_with_no_punctuation():
    long_no_punctuation = " ".join(f"word{i}" for i in range(25))
    assert _pending_speakable_chunks([long_no_punctuation], 0, is_final=False) == [
        long_no_punctuation
    ]


def test_pending_speakable_chunks_empty_input():
    assert _pending_speakable_chunks([], 0, is_final=False) == []
    assert _pending_speakable_chunks([], 0, is_final=True) == []


class _FakeTTS(TextToSpeechProvider):
    name = "fake"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[str] = []

    async def synthesize(self, text: str) -> SynthesisResult:
        self.calls.append(text)
        if self.fail:
            raise VoiceProviderError("fake tts failure")
        return SynthesisResult(audio=b"RIFF....WAVEfmt ", sample_rate=22050)


async def test_synthesize_chunk_falls_back_to_kokoro_when_fast_tts_fails():
    voice_providers = VoiceProviders()
    fallback = _FakeTTS()
    voice_providers.tts = fallback
    loop = GeminiLiveLoop(
        settings=Settings(),
        controller=object(),
        event_loop=asyncio.get_running_loop(),
        voice_providers=voice_providers,
    )
    loop._fast_tts = _FakeTTS(fail=True)

    result = await loop._synthesize_chunk("hello")

    assert result is not None
    assert fallback.calls == ["hello"]


async def test_synthesize_chunk_returns_none_when_both_providers_fail():
    voice_providers = VoiceProviders()
    voice_providers.tts = _FakeTTS(fail=True)
    loop = GeminiLiveLoop(
        settings=Settings(),
        controller=object(),
        event_loop=asyncio.get_running_loop(),
        voice_providers=voice_providers,
    )
    loop._fast_tts = _FakeTTS(fail=True)

    result, provider_name = await loop._synthesize_chunk("hello")

    assert result is None
    assert provider_name == ""
