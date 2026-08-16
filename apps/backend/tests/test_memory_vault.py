from __future__ import annotations

import aiosqlite
import pytest

from memory import fts
from memory.vault import reindex_vault
from storage.migrator import run_migrations


@pytest.fixture
async def conn(tmp_path):
    db_path = tmp_path / "vault_test.db"
    run_migrations(db_path)
    connection = await aiosqlite.connect(str(db_path))
    connection.row_factory = aiosqlite.Row
    yield connection
    await connection.close()


async def test_reindex_missing_vault_is_a_clean_noop(conn, tmp_path):
    result = await reindex_vault(conn, str(tmp_path / "does-not-exist"))
    assert result.vault_missing is True
    assert result.indexed == 0


async def test_reindex_indexes_markdown_files(conn, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "strategy.md").write_text("# Breakout Strategy\n\nWait for a clean break of range.", encoding="utf-8")

    result = await reindex_vault(conn, str(vault))
    assert result.indexed == 1
    assert result.vault_missing is False

    found = await fts.search(conn, "breakout", source="vault")
    assert len(found) == 1
    assert found[0]["source_id"] == "strategy.md"
    assert found[0]["title"] == "Breakout Strategy"


async def test_reindex_skips_unchanged_files_on_second_pass(conn, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("static content", encoding="utf-8")

    first = await reindex_vault(conn, str(vault))
    second = await reindex_vault(conn, str(vault))
    assert first.indexed == 1
    assert second.indexed == 0
    assert second.unchanged == 1


async def test_reindex_reindexes_changed_files(conn, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    note.write_text("original text about oil", encoding="utf-8")
    await reindex_vault(conn, str(vault))

    note.write_text("revised text about copper", encoding="utf-8")
    result = await reindex_vault(conn, str(vault))
    assert result.indexed == 1

    assert await fts.search(conn, "oil", source="vault") == []
    assert len(await fts.search(conn, "copper", source="vault")) == 1


async def test_reindex_removes_deleted_files(conn, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "temp.md"
    note.write_text("temporary note", encoding="utf-8")
    await reindex_vault(conn, str(vault))

    note.unlink()
    result = await reindex_vault(conn, str(vault))
    assert result.removed == 1
    assert await fts.search(conn, "temporary", source="vault") == []


async def test_reindex_excludes_obsidian_bookkeeping_dir(conn, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "workspace.md").write_text("internal", encoding="utf-8")
    (vault / "real-note.md").write_text("actual content", encoding="utf-8")

    result = await reindex_vault(conn, str(vault))
    assert result.indexed == 1
