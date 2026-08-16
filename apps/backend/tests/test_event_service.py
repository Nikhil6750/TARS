from __future__ import annotations

import asyncio
from uuid import uuid4

import aiosqlite
import pytest

from app.schemas import Direction, EventSource, EventState, TradingEvent, ValidationStatus
from events.service import DuplicateEventError, EventService
from storage.migrator import run_migrations


@pytest.fixture
async def conn(tmp_path):
    db_path = tmp_path / "events_test.db"
    run_migrations(db_path)
    connection = await aiosqlite.connect(str(db_path))
    connection.row_factory = aiosqlite.Row
    yield connection
    await connection.close()


def _setup_event(
    event_id=None,
    symbol: str = "XAUUSD",
    state: EventState = EventState.SETUP_VALID,
    validation_status: ValidationStatus = ValidationStatus.VALID,
    **overrides,
) -> TradingEvent:
    kwargs = dict(
        source=EventSource.manual,
        symbol=symbol,
        state=state,
        direction=Direction.LONG,
        entry=2400.0,
        stop_loss=2390.0,
        take_profit=2420.0,
        risk_reward=2.0,
        validation_status=validation_status,
    )
    kwargs.update(overrides)
    if event_id is not None:
        kwargs["event_id"] = event_id
    return TradingEvent(**kwargs)


async def test_unique_events_all_persist(conn):
    service = EventService(conn)
    a = _setup_event()
    b = _setup_event(symbol="ES")

    await service.record_event(a)
    await service.record_event(b)

    history = await service.get_history()
    assert {row["event_id"] for row in history} == {str(a.event_id), str(b.event_id)}


async def test_duplicate_event_id_rejected_and_original_row_unchanged(conn):
    service = EventService(conn)
    original = _setup_event()
    await service.record_event(original)

    duplicate = _setup_event(
        event_id=original.event_id,
        symbol="GBPUSD",
        state=EventState.IDLE,
        validation_status=ValidationStatus.EXPIRED,
        direction=None,
        entry=None,
        stop_loss=None,
        take_profit=None,
        risk_reward=None,
    )
    with pytest.raises(DuplicateEventError) as exc_info:
        await service.record_event(duplicate)
    assert exc_info.value.event_id == str(original.event_id)

    stored = await service.get_by_id(str(original.event_id))
    assert stored["symbol"] == "XAUUSD"
    assert stored["state"] == "SETUP_VALID"
    assert stored["validation_status"] == "VALID"

    history = await service.get_history()
    assert len(history) == 1

    # The rejected duplicate must not have touched active-setup state either.
    active = await service.get_active_setups()
    assert len(active) == 1
    assert active[0]["symbol"] == "XAUUSD"


async def test_lifecycle_three_events_stay_queryable_and_active_state_clears(conn):
    service = EventService(conn)
    developing = _setup_event(state=EventState.SETUP_DEVELOPING, validation_status=ValidationStatus.PENDING)
    await service.record_event(developing)

    valid = _setup_event(state=EventState.SETUP_VALID, validation_status=ValidationStatus.VALID)
    await service.record_event(valid)

    invalidated = _setup_event(state=EventState.SETUP_INVALIDATED, validation_status=ValidationStatus.INVALID)
    await service.record_event(invalidated)

    ids = {str(developing.event_id), str(valid.event_id), str(invalidated.event_id)}
    assert len(ids) == 3

    history = await service.get_history()
    history_ids = {row["event_id"] for row in history}
    assert ids <= history_ids
    assert len(history) == 3

    active = await service.get_active_setups()
    assert active == []


async def test_concurrent_duplicate_event_id_only_one_persists(conn):
    service = EventService(conn)
    shared_id = uuid4()
    first = _setup_event(event_id=shared_id, symbol="XAUUSD")
    second = _setup_event(event_id=shared_id, symbol="ES")

    results = await asyncio.gather(
        service.record_event(first), service.record_event(second), return_exceptions=True
    )

    successes = [r for r in results if not isinstance(r, BaseException)]
    failures = [r for r in results if isinstance(r, BaseException)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], DuplicateEventError)

    history = await service.get_history()
    matching = [row for row in history if row["event_id"] == str(shared_id)]
    assert len(matching) == 1
