"""AppStateStore + its migration -- round-trip get/set, including the
update-in-place path (mission section 4's `last_daily_brief_date`)."""
from __future__ import annotations

import aiosqlite
import pytest

from storage.app_state import AppStateStore
from storage.migrator import run_migrations


@pytest.fixture
async def conn(tmp_path):
    db_path = tmp_path / "app_state_test.db"
    run_migrations(db_path)
    connection = await aiosqlite.connect(str(db_path))
    connection.row_factory = aiosqlite.Row
    yield connection
    await connection.close()


async def test_get_missing_key_is_none(conn):
    store = AppStateStore(conn)
    assert await store.get("last_daily_brief_date") is None


async def test_set_then_get_round_trips(conn):
    store = AppStateStore(conn)
    await store.set("last_daily_brief_date", "2026-03-05")
    assert await store.get("last_daily_brief_date") == "2026-03-05"


async def test_set_overwrites_existing_value(conn):
    store = AppStateStore(conn)
    await store.set("last_daily_brief_date", "2026-03-05")
    await store.set("last_daily_brief_date", "2026-03-06")
    assert await store.get("last_daily_brief_date") == "2026-03-06"


async def test_keys_are_independent(conn):
    store = AppStateStore(conn)
    await store.set("last_daily_brief_date", "2026-03-05")
    await store.set("some_other_key", "x")
    assert await store.get("last_daily_brief_date") == "2026-03-05"
    assert await store.get("some_other_key") == "x"
