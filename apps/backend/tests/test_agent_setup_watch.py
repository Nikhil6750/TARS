from __future__ import annotations

import aiosqlite
import pytest

from agents.models import AgentRunStatus
from agents.setup_watch_agent import SetupWatchAgent
from app.schemas import Direction, EventSource, EventState, TradingEvent, ValidationStatus
from events.service import EventService
from memory.service import KIND_DECISION, MemoryService
from storage.migrator import run_migrations
from trading.context import TradingContextBuilder
from trading.models import StrategyStatus
from trading.provider import NullStrategyProvider


@pytest.fixture
async def conn(tmp_path):
    db_path = tmp_path / "setup_watch_agent_test.db"
    run_migrations(db_path)
    connection = await aiosqlite.connect(str(db_path))
    connection.row_factory = aiosqlite.Row
    yield connection
    await connection.close()


@pytest.fixture
async def memory(conn, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    return MemoryService(conn, vault_path=str(vault), sqlite_vec_enabled=False)


@pytest.fixture
def events(conn):
    return EventService(conn)


@pytest.fixture
def context_builder(events):
    # NullStrategyProvider is the real default (see trading/provider.py) --
    # this is what "no strategy source wired in" actually looks like, not a
    # test-only stand-in.
    return TradingContextBuilder(events, NullStrategyProvider())


def _event(
    symbol: str = "AAPL",
    state: EventState = EventState.SETUP_VALID,
    *,
    direction: Direction = Direction.LONG,
    entry: float = 150.0,
    stop_loss: float = 148.0,
    take_profit: float = 155.0,
    risk_reward: float = 2.5,
    validation_status: ValidationStatus = ValidationStatus.VALID,
    reason_codes: list[str] | None = None,
) -> TradingEvent:
    return TradingEvent(
        source=EventSource.mock,
        symbol=symbol,
        state=state,
        direction=direction,
        entry=entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        risk_reward=risk_reward,
        validation_status=validation_status,
        reason_codes=reason_codes or ["BREAKOUT"],
    )


async def test_new_active_setup_is_recorded_as_a_decision(events, memory, context_builder):
    await events.record_event(_event())
    agent = SetupWatchAgent(context_builder, memory)

    result = await agent.run_once()

    assert result.status == AgentRunStatus.SUCCEEDED
    assert result.summary == "1 setup change(s) observed"
    notes = await memory.list_notes(KIND_DECISION)
    assert len(notes) == 1
    assert "AAPL" in notes[0]["body"]
    assert notes[0]["actor"] == "agent:setup_watch_agent"


async def test_zero_changes_is_a_successful_iteration_not_a_failure(events, memory, context_builder):
    agent = SetupWatchAgent(context_builder, memory)

    result = await agent.run_once()

    assert result.status == AgentRunStatus.SUCCEEDED
    assert result.summary == "0 setup change(s) observed"
    notes = await memory.list_notes(KIND_DECISION)
    assert notes == []


async def test_unchanged_setup_on_second_call_produces_no_new_decision(events, memory, context_builder):
    await events.record_event(_event())
    agent = SetupWatchAgent(context_builder, memory)
    await agent.run_once()

    result = await agent.run_once()

    assert result.summary == "0 setup change(s) observed"
    notes = await memory.list_notes(KIND_DECISION)
    assert len(notes) == 1  # still just the one note from the first iteration


async def test_state_change_triggers_a_new_decision(events, memory, context_builder):
    await events.record_event(
        _event(state=EventState.SETUP_DEVELOPING, validation_status=ValidationStatus.PENDING)
    )
    agent = SetupWatchAgent(context_builder, memory)
    await agent.run_once()

    await events.record_event(_event(state=EventState.SETUP_VALID, validation_status=ValidationStatus.VALID))
    result = await agent.run_once()

    assert result.summary == "1 setup change(s) observed"
    notes = await memory.list_notes(KIND_DECISION)
    assert len(notes) == 2


async def test_not_configured_strategy_status_never_implies_a_verdict(events, memory, context_builder):
    """CRITICAL CONSTRAINT: with the default NullStrategyProvider
    (strategy_status == NOT_CONFIGURED), the saved decision text must
    restate only deterministic fields -- no recommendation, confidence
    score, or buy/sell verdict language layered on top."""
    await events.record_event(_event())
    agent = SetupWatchAgent(context_builder, memory)

    result = await agent.run_once()

    assert result.data["strategy_status"] == StrategyStatus.NOT_CONFIGURED.value
    notes = await memory.list_notes(KIND_DECISION)
    body = notes[0]["body"].lower()

    # Checked against the actual wording _describe_change() produces (see
    # agents/setup_watch_agent.py), not an abstract disconnected blocklist.
    forbidden = [
        "recommend",
        "confidence",
        "signal",
        "should",
        " buy",
        " sell",
        "guarantee",
        "prediction",
    ]
    for word in forbidden:
        assert word not in body, f"unexpected verdict-implying language {word!r} in: {body!r}"

    assert "no strategy configured" in body
    assert "aapl" in body
    assert "setup_valid" in body
