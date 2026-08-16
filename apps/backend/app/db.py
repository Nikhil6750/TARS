"""Async SQLite access. A single shared `aiosqlite` connection is opened at
app startup (WAL mode, foreign keys on) and reused — appropriate for the
single-user local companion described in ARCHITECTURE.md, not a pool built
for concurrent multi-tenant load.
"""
from __future__ import annotations

from pathlib import Path

import aiosqlite

from app.config import Settings
from storage.migrator import run_migrations


class Database:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected — call connect() first")
        return self._conn

    async def connect(self) -> None:
        run_migrations(self.db_path)
        self._conn = await aiosqlite.connect(str(self.db_path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None


def build_database(settings: Settings) -> Database:
    return Database(settings.sqlite_path)
