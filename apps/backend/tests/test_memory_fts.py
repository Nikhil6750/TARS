from __future__ import annotations

import aiosqlite
import pytest

from memory import fts
from storage.migrator import run_migrations


@pytest.fixture
async def conn(tmp_path):
    db_path = tmp_path / "fts_test.db"
    run_migrations(db_path)
    connection = await aiosqlite.connect(str(db_path))
    connection.row_factory = aiosqlite.Row
    yield connection
    await connection.close()


def test_build_match_query_quotes_tokens():
    assert fts.build_match_query('risk "warning" (test)') == '"risk" AND "warning" AND "test"'


def test_build_match_query_empty_input_returns_none():
    assert fts.build_match_query("   !!! ") is None


async def test_upsert_and_search_roundtrip(conn):
    await fts.upsert(conn, source="vault", source_id="notes/a.md", title="Risk notes", body="Never risk more than one percent per trade.")
    results = await fts.search(conn, "risk")
    assert len(results) == 1
    assert results[0]["source"] == "vault"
    assert results[0]["source_id"] == "notes/a.md"


async def test_search_filters_by_source(conn):
    await fts.upsert(conn, source="vault", source_id="a.md", title="A", body="breakout strategy notes")
    await fts.upsert(conn, source="conversation", source_id="msg-1", title="user", body="breakout question")
    only_vault = await fts.search(conn, "breakout", source="vault")
    assert [r["source"] for r in only_vault] == ["vault"]


async def test_upsert_replaces_existing_entry_for_same_source_id(conn):
    await fts.upsert(conn, source="vault", source_id="a.md", title="A", body="old content about gold")
    await fts.upsert(conn, source="vault", source_id="a.md", title="A", body="new content about silver")
    results = await fts.search(conn, "gold")
    assert results == []
    results = await fts.search(conn, "silver")
    assert len(results) == 1


async def test_search_query_with_special_characters_does_not_raise(conn):
    await fts.upsert(conn, source="vault", source_id="a.md", title="A", body="normal text")
    results = await fts.search(conn, '"(unbalanced quote')
    assert isinstance(results, list)
