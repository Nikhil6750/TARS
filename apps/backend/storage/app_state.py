"""AppStateStore -- tiny persistent key-value store (see migration
0010_app_state.sql) for small runtime facts that must survive a backend
restart but are not conversation memory and not env-sourced Settings.
First use: `last_daily_brief_date` (mission section 4).
"""
from __future__ import annotations

from datetime import UTC, datetime


class AppStateStore:
    def __init__(self, conn) -> None:
        self._conn = conn

    async def get(self, key: str) -> str | None:
        cursor = await self._conn.execute("SELECT value FROM app_state WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row["value"] if row else None

    async def set(self, key: str, value: str) -> None:
        await self._conn.execute(
            "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, datetime.now(UTC).isoformat()),
        )
        await self._conn.commit()
