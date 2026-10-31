"""Replay must not see a bar before it has closed.

A bar's ``ts`` is its open, so ``ts <= as_of`` returned the bar still forming
at the cursor, including prices from after it. These tests use a store whose
prices encode the minute they belong to, so any leak is visible as a number
rather than inferred.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import duckdb
import pytest

from market import universe
from market.store import CandleStore

START = datetime(2026, 3, 2, 0, 0, tzinfo=UTC)
HOURS = 6


@pytest.fixture
def store(tmp_path):
    """EURUSD whose every close equals the minute offset of the bar's end."""
    for timeframe, minutes in universe.TIMEFRAMES.items():
        directory = tmp_path / "EURUSD"
        directory.mkdir(exist_ok=True)
        count = HOURS * 60 // minutes
        rows = ", ".join(
            f"(TIMESTAMP '{(START + timedelta(minutes=i * minutes)).strftime('%Y-%m-%d %H:%M:%S')}',"
            f" 1.0, 1.0, 1.0, {float((i + 1) * minutes)}, 0.0, 1.0)"
            for i in range(max(count, 1))
        )
        target = str(directory / f"{timeframe}.parquet").replace("'", "''")
        duckdb.sql(
            f"COPY (SELECT * FROM (VALUES {rows}) "
            "t(ts, open, high, low, close, spread, volume)) "
            f"TO '{target}' (FORMAT PARQUET)"
        )
    return CandleStore(tmp_path)


def at(hours: int, minutes: int = 0) -> datetime:
    return START + timedelta(hours=hours, minutes=minutes)


def test_a_bar_still_forming_at_the_cursor_is_not_visible(store):
    """The defect: at 03:05 the H1 bar that opened at 03:00 ran to 03:59."""
    bars = store.candles("EURUSD", "H1", as_of=at(3, 5), limit=10)
    assert bars[-1].ts == at(2)
    assert bars[-1].close == 180.0


def test_a_cursor_on_the_boundary_sees_the_bar_that_just_closed_and_no_further(store):
    bars = store.candles("EURUSD", "H1", as_of=at(3), limit=10)
    assert bars[-1].ts == at(2)


def test_a_bar_becomes_visible_the_moment_it_closes(store):
    assert store.candles("EURUSD", "H1", as_of=at(4) - timedelta(seconds=1), limit=1)[-1].ts == at(2)
    assert store.candles("EURUSD", "H1", as_of=at(4), limit=1)[-1].ts == at(3)


@pytest.mark.parametrize("timeframe", ["M5", "M15", "H1", "H4"])
def test_no_timeframe_returns_a_close_from_after_the_cursor(store, timeframe):
    cursor = at(4, 7)
    limit_minutes = (cursor - START).total_seconds() / 60
    for bar in store.candles("EURUSD", timeframe, as_of=cursor, limit=1000):
        assert bar.close <= limit_minutes, f"{timeframe} bar {bar.ts} leaks a future close"


def test_close_series_and_latest_close_obey_the_same_rule(store):
    cursor = at(3, 5)
    assert store.close_series("EURUSD", "H1", as_of=cursor, limit=10)[-1][1] == 180.0
    assert store.latest_close("EURUSD", "H1", as_of=cursor).close == 180.0


def test_nothing_is_visible_before_the_first_bar_closes(store):
    assert store.candles("EURUSD", "H1", as_of=at(0, 30), limit=10) == []


def test_the_bounds_end_at_the_close_of_the_last_bar_so_it_stays_reachable(store):
    _, last = store.store_bounds("M5")
    assert last == at(HOURS)
    assert store.candles("EURUSD", "M5", as_of=last, limit=1)[-1].ts == at(HOURS) - timedelta(minutes=5)
