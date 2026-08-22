from __future__ import annotations

import asyncio

import aiosqlite
import pytest

from app.config import Settings
from app.schemas import InputMode
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
)
from storage.migrator import run_migrations


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


class NeverAdvanced:
    async def handle_text_stream(self, *_args, **_kwargs):
        raise AssertionError("ordinary conversation entered the advanced runtime")
        yield


class NeverUsed:
    def __getattr__(self, name):
        raise AssertionError(f"unexpected dependency use: {name}")


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
