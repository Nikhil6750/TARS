"""Memory/retrieval service — the single entry point the rest of the
backend uses for indexing and searching memory. Enforces the boundary from
ARCHITECTURE.md § Memory architecture: operational state (trading_events,
active_setups) is never indexed here, conversation memory and vault
research notes are searchable but are never treated as validated trading
performance, and every result carries its source + source_id so an
assistant answer can cite exactly where it came from.
"""
from __future__ import annotations

from typing import Any

import aiosqlite

from app.schemas import AssistantMessage
from memory import fts
from memory.vault import VaultIndexResult, reindex_vault


class MemoryService:
    def __init__(self, conn: aiosqlite.Connection, vault_path: str, sqlite_vec_enabled: bool):
        self._conn = conn
        self._vault_path = vault_path
        if sqlite_vec_enabled:
            # ADR-013: sqlite-vec is added only if FTS5 relevance proves
            # insufficient in practice, not introduced speculatively. No
            # measurement has justified it yet, so refuse rather than
            # silently ignoring the flag.
            raise NotImplementedError(
                "SQLITE_VEC_ENABLED=true but semantic retrieval is not yet "
                "implemented — see ARCHITECTURE.md § Memory architecture "
                "and DECISIONS.md ADR-013. Set it back to false."
            )

    async def index_conversation_message(self, message: AssistantMessage) -> None:
        title = f"{message.role.value} @ {message.timestamp.isoformat()}"
        await fts.upsert(
            self._conn,
            source="conversation",
            source_id=str(message.message_id),
            title=title,
            body=message.content,
        )

    async def reindex_vault(self) -> VaultIndexResult:
        return await reindex_vault(self._conn, self._vault_path)

    async def search(
        self, query: str, limit: int = 10, source: str | None = None
    ) -> list[dict[str, Any]]:
        return await fts.search(self._conn, query=query, limit=limit, source=source)
