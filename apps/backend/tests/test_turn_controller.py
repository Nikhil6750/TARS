from __future__ import annotations

import asyncio

import aiosqlite
import pytest

from app.config import Settings
from app.schemas import InputMode
from app.voice_telemetry import VoiceTraceStore
from assistant.conversation_store import ConversationStore
from assistant.provider import AssistantProvider, AssistantReply, AssistantRequest
from assistant.turn_controller import (
    AssistantTurnController,
    DuplicateTurnConflict,
    TurnIntent,
    TurnIntentRouter,
    TurnStatus,
    WakePhraseMatcher,
    normalize_transcript,
    sentence_chunks,
)
from storage.migrator import run_migrations
from voice.audio_utils import pcm16_to_wav
from voice.interfaces import (
    SpeechToTextProvider,
    SynthesisResult,
    TextToSpeechProvider,
    TranscriptionResult,
)


class CountingProvider(AssistantProvider):
    name = "counting"

    def __init__(self) -> None:
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def respond(self, request: AssistantRequest) -> AssistantReply:
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return AssistantReply(text=f"Answer: {request.text}", provider=self.name)


class FlakyOnceProvider(AssistantProvider):
    """Always returns an empty result -- reproduces the physically observed
    Claude Code CLI behavior of exiting cleanly with no usable result text
    on an occasional call. Used alone (no RoutedAssistantProvider wrapping)
    to prove the turn controller no longer retries the same flaky provider
    itself -- cross-provider fallback is the router's job now, see
    test_provider_router_v2.py."""

    name = "flaky"

    def __init__(self) -> None:
        self.calls = 0

    async def respond(self, request: AssistantRequest) -> AssistantReply:
        self.calls += 1
        return AssistantReply(text="", provider=self.name)


class NeverAdvanced:
    async def handle_text_stream(self, *_args, **_kwargs):
        raise AssertionError("ordinary conversation entered the advanced runtime")
        yield


class NeverUsed:
    def __getattr__(self, name):
        raise AssertionError(f"unexpected dependency use: {name}")


class _FixedTranscriptSTT(SpeechToTextProvider):
    name = "fixed"
    sample_rate = 16000

    def __init__(self, text: str) -> None:
        self._text = text

    async def transcribe(self, pcm_audio: bytes) -> TranscriptionResult:
        return TranscriptionResult(text=self._text, language="en")


class _SilentTTS(TextToSpeechProvider):
    name = "silent"

    async def synthesize(self, text: str) -> SynthesisResult:
        return SynthesisResult(audio=pcm16_to_wav(b"\x00\x00" * 100, 16000), sample_rate=16000)


class _FakeVoiceProviders:
    def __init__(self, stt: SpeechToTextProvider, tts: TextToSpeechProvider) -> None:
        self.stt = stt
        self.tts = tts


@pytest.fixture
async def voice_controller(tmp_path):
    db_path = tmp_path / "voice_turns.db"
    run_migrations(db_path)
    conn = await aiosqlite.connect(str(db_path))
    conn.row_factory = aiosqlite.Row
    provider = CountingProvider()

    def make(voice_mode: str, transcript: str) -> tuple[AssistantTurnController, CountingProvider]:
        voice = _FakeVoiceProviders(stt=_FixedTranscriptSTT(transcript), tts=_SilentTTS())
        instance = AssistantTurnController(
            settings=Settings(database_url=f"sqlite:///{db_path}", voice_mode=voice_mode),
            provider=provider,
            assistant_router=NeverUsed(),
            orchestrator=NeverAdvanced(),
            action_runtime=NeverUsed(),
            conversation_store=ConversationStore(conn),
            hot_chart_state_store=NeverUsed(),
            voice_providers=voice,
            voice_trace_store=VoiceTraceStore(conn),
        )
        return instance, provider

    yield make
    await conn.close()


async def test_continuous_mode_executes_recognized_speech_without_wake_phrase(voice_controller):
    controller, provider = voice_controller("continuous", "explain docker containers")
    provider.release.set()

    response = await controller.execute_utterance(
        b"\x00\x00" * 3200,
        conversation_id="conv-continuous",
        session_id="session-continuous",
        turn_id="turn-continuous-1",
    )

    assert response.status is TurnStatus.COMPLETED
    assert response.intent is TurnIntent.NORMAL_CONVERSATION
    assert provider.calls == 1


async def test_default_wake_mode_ignores_speech_without_wake_phrase(voice_controller):
    controller, provider = voice_controller("wake", "explain docker containers")
    provider.release.set()

    response = await controller.execute_utterance(
        b"\x00\x00" * 3200,
        conversation_id="conv-wake",
        session_id="session-wake",
        turn_id="turn-wake-1",
    )

    assert response.status is TurnStatus.IGNORED
    assert provider.calls == 0


@pytest.fixture
async def controller(tmp_path):
    db_path = tmp_path / "turns.db"
    run_migrations(db_path)
    conn = await aiosqlite.connect(str(db_path))
    conn.row_factory = aiosqlite.Row
    provider = CountingProvider()
    instance = AssistantTurnController(
        settings=Settings(database_url=f"sqlite:///{db_path}"),
        provider=provider,
        assistant_router=NeverUsed(),
        orchestrator=NeverAdvanced(),
        action_runtime=NeverUsed(),
        conversation_store=ConversationStore(conn),
        hot_chart_state_store=NeverUsed(),
    )
    yield instance, provider
    await conn.close()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("open calculator", TurnIntent.DETERMINISTIC),
        ("what is polymorphism?", TurnIntent.NORMAL_CONVERSATION),
        ("analyze the chart", TurnIntent.CHART_ANALYSIS),
        ("create a file called notes.txt", TurnIntent.TOOL_TASK),
        ("research current ECB policy with sources", TurnIntent.RESEARCH),
        ("run a walk-forward strategy research study", TurnIntent.TRADING_RESEARCH),
        # Natural phrasing with a trailing word ("today") previously fell
        # through the anchored _DATE/_TIME regexes to the flaky
        # NORMAL_CONVERSATION/Claude Code path instead of the reliable
        # deterministic date/time handler -- observed physically.
        ("what is the date today", TurnIntent.DETERMINISTIC),
        ("what's the date today", TurnIntent.DETERMINISTIC),
        ("what day is it today", TurnIntent.DETERMINISTIC),
        ("what is today's date", TurnIntent.DETERMINISTIC),
        ("what time is it right now", TurnIntent.DETERMINISTIC),
        ("what's the time currently", TurnIntent.DETERMINISTIC),
        ("what is the date of the docker release", TurnIntent.NORMAL_CONVERSATION),
    ],
)
def test_small_router_is_deterministic(text, expected):
    assert TurnIntentRouter().classify(text) is expected


@pytest.mark.parametrize(
    ("transcript", "command"),
    [
        ("Hey TARS", ""),
        ("Hey TARS, analyze the chart", "analyze the chart"),
        ("hey tarz explain Docker", "explain docker"),
        ("Hey stars! What time is it?", "what time is it"),
        ("TARS: explain inheritance", "explain inheritance"),
        ("Jarvis, open calculator", "open calculator"),
        ("Hey Jarvis explain polymorphism", "explain polymorphism"),
        ("Hey Tar's, are you there?", "are you there"),
    ],
)
def test_configurable_wake_aliases_preserve_one_shot_command_tail(transcript, command):
    matcher = WakePhraseMatcher(
        ["hey tars", "hey tarz", "hey stars", "tars", "jarvis", "hey jarvis"]
    )
    match = matcher.match(transcript)
    assert match is not None
    assert match.command == command


def test_wake_normalization_does_not_match_unrelated_words():
    assert normalize_transcript("  HEY,   TAR'S!!! ") == "hey tars"
    matcher = WakePhraseMatcher(["tars"])
    assert matcher.match("The stars are bright") is None


def test_speech_is_chunked_only_on_complete_sentence_boundaries():
    assert sentence_chunks("First sentence. Second question? Final statement!") == [
        "First sentence.",
        "Second question?",
        "Final statement!",
    ]
    assert sentence_chunks("One final fragment without punctuation") == [
        "One final fragment without punctuation"
    ]


async def test_normal_conversation_fast_path_has_one_execution(controller):
    turn_controller, provider = controller
    provider.release.set()

    result = await turn_controller.execute_text(
        "explain polymorphism",
        conversation_id="e57b1ba8-6c31-4d52-9f69-9d5897a51d8b",
        turn_id="turn-once",
    )

    assert result.turn_id == "turn-once"
    assert result.intent is TurnIntent.NORMAL_CONVERSATION
    assert result.status is TurnStatus.COMPLETED
    assert result.provider == "counting"
    assert result.display_text == "Answer: explain polymorphism"
    assert result.speech_text == "Answer: explain polymorphism"
    assert provider.calls == 1
    assert turn_controller.execution_count("turn-once") == 1


async def test_normal_conversation_does_not_retry_same_flaky_provider(tmp_path):
    """An empty response is not silently retried against the same provider
    -- that's the router's job (falling over to a different, healthy
    provider within one call, see test_provider_router_v2.py). With only
    one, always-empty provider wired directly (no router), the turn must
    fail immediately rather than waste a second call on a provider that
    already proved it has nothing to say."""
    db_path = tmp_path / "flaky_turns.db"
    run_migrations(db_path)
    conn = await aiosqlite.connect(str(db_path))
    conn.row_factory = aiosqlite.Row
    provider = FlakyOnceProvider()
    turn_controller = AssistantTurnController(
        settings=Settings(database_url=f"sqlite:///{db_path}"),
        provider=provider,
        assistant_router=NeverUsed(),
        orchestrator=NeverAdvanced(),
        action_runtime=NeverUsed(),
        conversation_store=ConversationStore(conn),
        hot_chart_state_store=NeverUsed(),
    )

    result = await turn_controller.execute_text(
        "what is the date of the docker release",
        conversation_id="a1a1a1a1-1111-4111-8111-111111111111",
        turn_id="turn-flaky",
    )

    assert result.status is TurnStatus.FAILED
    assert provider.calls == 1

    await conn.close()


async def test_same_turn_id_joins_inflight_and_replays_without_second_call(controller):
    turn_controller, provider = controller
    first = asyncio.create_task(
        turn_controller.execute_text(
            "explain inheritance",
            conversation_id="e57b1ba8-6c31-4d52-9f69-9d5897a51d8b",
            turn_id="duplicate-turn",
        )
    )
    await asyncio.sleep(0)
    second = asyncio.create_task(
        turn_controller.execute_text(
            "explain inheritance",
            conversation_id="e57b1ba8-6c31-4d52-9f69-9d5897a51d8b",
            turn_id="duplicate-turn",
        )
    )
    await asyncio.wait_for(provider.entered.wait(), timeout=1)
    assert provider.calls == 1

    provider.release.set()
    first_result, second_result = await asyncio.gather(first, second)
    replay = await turn_controller.execute_text(
        "explain inheritance",
        conversation_id="e57b1ba8-6c31-4d52-9f69-9d5897a51d8b",
        turn_id="duplicate-turn",
    )

    assert first_result.display_text == second_result.display_text
    assert replay.replayed is True
    assert provider.calls == 1
    assert turn_controller.execution_count("duplicate-turn") == 1


async def test_turn_id_cannot_be_reused_for_different_input(controller):
    turn_controller, provider = controller
    provider.release.set()
    await turn_controller.execute_text(
        "first question",
        conversation_id="e57b1ba8-6c31-4d52-9f69-9d5897a51d8b",
        turn_id="conflict-turn",
        input_mode=InputMode.text,
    )

    with pytest.raises(DuplicateTurnConflict):
        await turn_controller.execute_text(
            "different question",
            conversation_id="e57b1ba8-6c31-4d52-9f69-9d5897a51d8b",
            turn_id="conflict-turn",
            input_mode=InputMode.text,
        )
