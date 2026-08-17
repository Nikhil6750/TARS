"""Low-level SQLite access for `memory_notes` — the structured/provenance
side of memory (explicit "remember this" facts, trading observations,
task/decision records). `memory_fts` (memory/fts.py) remains the only
full-text index; this module is the structured record those FTS rows point
back to via matching `source`/`source_id`. Nothing above this module writes
to `memory_notes` directly; `memory/service.py` is the only caller.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import aiosqlite


async def insert(
    conn: aiosqlite.Connection,
    *,
    kind: str,
    actor: str,
    body: str,
    conversation_id: str | None = None,
    symbol: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    note_id: str | None = None,
    created_at: datetime | None = None,
) -> str:
    note_id = note_id or uuid4().hex
    created = (created_at or datetime.now(UTC)).isoformat()
    await conn.execute(
        """
        INSERT INTO memory_notes (
            note_id, kind, source_id, actor, conversation_id, symbol,
            tags, body, metadata, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            note_id,
            kind,
            note_id,
            actor,
            conversation_id,
            symbol,
            json.dumps(tags or []),
            body,
            json.dumps(metadata or {}),
            created,
        ),
    )
    await conn.commit()
    return note_id


async def get(conn: aiosqlite.Connection, note_id: str) -> dict[str, Any] | None:
    cursor = await conn.execute("SELECT * FROM memory_notes WHERE note_id = ?", (note_id,))
    row = await cursor.fetchone()
    return _row_to_dict(row) if row else None


async def list_by_kind(
    conn: aiosqlite.Connection,
    kind: str,
    *,
    symbol: str | None = None,
    conversation_id: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 200))
    sql = "SELECT * FROM memory_notes WHERE kind = ?"
    params: list[Any] = [kind]
    if symbol:
        sql += " AND symbol = ?"
        params.append(symbol.upper())
    if conversation_id:
        sql += " AND conversation_id = ?"
        params.append(conversation_id)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    cursor = await conn.execute(sql, params)
    rows = await cursor.fetchall()
    return [_row_to_dict(row) for row in rows]


async def delete(conn: aiosqlite.Connection, note_id: str) -> bool:
    cursor = await conn.execute("SELECT 1 FROM memory_notes WHERE note_id = ?", (note_id,))
    existed = await cursor.fetchone() is not None
    await conn.execute("DELETE FROM memory_notes WHERE note_id = ?", (note_id,))
    await conn.commit()
    return existed


def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "note_id": row["note_id"],
        "kind": row["kind"],
        "actor": row["actor"],
        "conversation_id": row["conversation_id"],
        "symbol": row["symbol"],
        "tags": json.loads(row["tags"]),
        "body": row["body"],
        "metadata": json.loads(row["metadata"]),
        "created_at": row["created_at"],
    }
