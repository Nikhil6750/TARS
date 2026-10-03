"""WatchlistStore -- persisted market watchlist (mission: TARS Watcher
Intelligence, section 2/23A). Uses the real AppStateStore/migration, same
fixture pattern as test_app_state.py, so these tests also exercise the real
JSON persistence contract, not a mock."""
from __future__ import annotations

import aiosqlite
import pytest

from storage.app_state import AppStateStore
from storage.migrator import run_migrations
from trading.watchlist import WatchlistStore


@pytest.fixture
async def conn(tmp_path):
    db_path = tmp_path / "watchlist_test.db"
    run_migrations(db_path)
    connection = await aiosqlite.connect(str(db_path))
    connection.row_factory = aiosqlite.Row
    yield connection
    await connection.close()


@pytest.fixture
def store(conn):
    return WatchlistStore(AppStateStore(conn))


async def test_empty_watchlist_on_a_fresh_store(store):
    assert await store.load() == []


async def test_add_then_load_round_trips(store):
    entry = await store.add("gold", "XAUUSD")
    assert entry.canonical_symbol == "XAUUSD"
    assert entry.asset == "gold"
    assert entry.added_at  # non-empty timestamp
    loaded = await store.load()
    assert [e.canonical_symbol for e in loaded] == ["XAUUSD"]


async def test_adding_the_same_symbol_again_updates_not_duplicates(store):
    await store.add("gold", "XAUUSD")
    await store.add("gold spot", "XAUUSD")
    loaded = await store.load()
    assert len(loaded) == 1
    assert loaded[0].asset == "gold spot"  # most recent wins


async def test_remove_drops_the_entry(store):
    await store.add("gold", "XAUUSD")
    await store.add("euro dollar", "EURUSD")
    removed = await store.remove("XAUUSD")
    assert removed is True
    loaded = await store.load()
    assert [e.canonical_symbol for e in loaded] == ["EURUSD"]


async def test_removing_an_unwatched_symbol_reports_false(store):
    assert await store.remove("XAUUSD") is False


async def test_watchlist_survives_a_fresh_store_instance_against_the_same_connection(conn):
    """Simulates surviving a backend restart: a brand new WatchlistStore
    (and AppStateStore) built against the same durable connection must see
    what an earlier instance persisted."""
    first = WatchlistStore(AppStateStore(conn))
    await first.add("bitcoin", "BTCUSD")

    second = WatchlistStore(AppStateStore(conn))
    loaded = await second.load()
    assert [e.canonical_symbol for e in loaded] == ["BTCUSD"]
