from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite
import pytest

from assistant.chart_analysis import ChartAnalysisResult
from assistant.hot_chart_state import ChartIdentity, HotChartState
from assistant.hot_chart_state_store import HotChartStateStore
from storage.migrator import run_migrations


@pytest.fixture
async def conn(tmp_path):
    db_path = tmp_path / "hot_chart_state_test.db"
    run_migrations(db_path)
    connection = await aiosqlite.connect(str(db_path))
    connection.row_factory = aiosqlite.Row
    yield connection
    await connection.close()


def _result(**overrides) -> ChartAnalysisResult:
    base = dict(
        instrument="XAUUSD",
        timeframe="15M",
        market_context="Ranging near resistance.",
        key_levels=["Resistance: 4550", "Support: 4500"],
        possible_setup=None,
        invalidation="Break below 4500",
        risk_notes="Macro/event risk has not been checked in this chart-only analysis.",
        provider="claude_code",
        raw_text="{}",
        structured=True,
    )
    base.update(overrides)
    return ChartAnalysisResult(**base)


def _state(*, chart_window_id="hwnd-1", symbol="XAUUSD", timeframe="15M") -> HotChartState:
    now = datetime.now(UTC).isoformat()
    return HotChartState(
        identity=ChartIdentity(chart_window_id=chart_window_id, symbol=symbol, timeframe=timeframe),
        analysis=_result(instrument=symbol, timeframe=timeframe),
        screenshot_hash="hash-1",
        source="vision",
        observed_at=now,
        analyzed_at=now,
    )


async def test_upsert_then_get_round_trips(conn):
    store = HotChartStateStore(conn)
    state = _state()
    await store.upsert(state)

    fetched = await store.get(state.identity)
    assert fetched is not None
    assert fetched.identity == state.identity
    assert fetched.analysis.instrument == "XAUUSD"
    assert fetched.analysis.key_levels == ["Resistance: 4550", "Support: 4500"]
    assert fetched.version == 1


async def test_get_returns_none_for_unknown_identity(conn):
    store = HotChartStateStore(conn)
    identity = ChartIdentity(chart_window_id="nope", symbol="XAUUSD", timeframe="15M")
    assert await store.get(identity) is None


async def test_get_rejects_symbol_mismatch(conn):
    store = HotChartStateStore(conn)
    await store.upsert(_state(symbol="XAUUSD"))

    mismatched = ChartIdentity(chart_window_id="hwnd-1", symbol="EURUSD", timeframe="15M")
    assert await store.get(mismatched) is None


async def test_get_rejects_timeframe_mismatch(conn):
    store = HotChartStateStore(conn)
    await store.upsert(_state(timeframe="15M"))

    mismatched = ChartIdentity(chart_window_id="hwnd-1", symbol="XAUUSD", timeframe="5M")
    assert await store.get(mismatched) is None


async def test_upsert_increments_version_on_repeat_writes_for_same_identity(conn):
    store = HotChartStateStore(conn)
    first = await store.upsert(_state())
    assert first.version == 1

    second = await store.upsert(_state())
    assert second.version == 2

    fetched = await store.get(second.identity)
    assert fetched.version == 2


async def test_different_symbols_on_same_window_do_not_collide(conn):
    store = HotChartStateStore(conn)
    await store.upsert(_state(symbol="XAUUSD"))
    await store.upsert(_state(symbol="EURUSD"))

    xau = await store.get(ChartIdentity(chart_window_id="hwnd-1", symbol="XAUUSD", timeframe="15M"))
    eur = await store.get(ChartIdentity(chart_window_id="hwnd-1", symbol="EURUSD", timeframe="15M"))
    assert xau is not None and xau.analysis.instrument == "XAUUSD"
    assert eur is not None and eur.analysis.instrument == "EURUSD"
    assert xau.version == 1
    assert eur.version == 1


async def test_none_symbol_and_timeframe_round_trip_correctly(conn):
    store = HotChartStateStore(conn)
    state = _state(symbol=None, timeframe=None)
    await store.upsert(state)

    fetched = await store.get(ChartIdentity(chart_window_id="hwnd-1", symbol=None, timeframe=None))
    assert fetched is not None
    assert fetched.identity.symbol is None
    assert fetched.identity.timeframe is None


async def test_delete_removes_the_row(conn):
    store = HotChartStateStore(conn)
    state = _state()
    await store.upsert(state)
    await store.delete(state.identity)

    assert await store.get(state.identity) is None
