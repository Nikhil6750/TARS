"""Low-level SQLite FTS5 access for `memory_fts` — full-text search over
conversation memory and the indexed Obsidian vault, per ARCHITECTURE.md
§ Memory architecture. Nothing above this module writes to `memory_fts`
directly; `memory/service.py` is the only caller.
"""
from __future__ import annotations

import re
from typing import Any

import aiosqlite

# FTS5's MATCH syntax treats characters like `"`, `-`, `*`, `(`, `)` as query
# operators. User/vault text passed straight through can throw a syntax
# error (or silently mean something other than "search for these words").
# Quoting each token turns the query into a plain AND-of-literals search —
# safe for any input, matching only whole tokens per FTS5's tokenizer.
_TOKEN_PATTERN = re.compile(r"[\w']+", re.UNICODE)


def build_match_query(text: str) -> str | None:
    tokens = _TOKEN_PATTERN.findall(text)
    if not tokens:
        return None
    return " AND ".join(f'"{t}"' for t in tokens)


async def upsert(
    conn: aiosqlite.Connection, source: str, source_id: str, title: str, body: str
) -> None:
    await conn.execute(
        "DELETE FROM memory_fts WHERE source = ? AND source_id = ?", (source, source_id)
    )
    await conn.execute(
        "INSERT INTO memory_fts (source, source_id, title, body) VALUES (?, ?, ?, ?)",
        (source, source_id, title, body),
    )
    await conn.commit()


async def delete(conn: aiosqlite.Connection, source: str, source_id: str) -> None:
    await conn.execute(
        "DELETE FROM memory_fts WHERE source = ? AND source_id = ?", (source, source_id)
    )
    await conn.commit()


async def search(
    conn: aiosqlite.Connection,
    query: str,
    limit: int = 10,
    source: str | None = None,
) -> list[dict[str, Any]]:
    match_query = build_match_query(query)
    if match_query is None:
        return []
    limit = max(1, min(limit, 100))

    sql = (
        "SELECT source, source_id, title, "
        "snippet(memory_fts, 3, '[', ']', ' ... ', 12) AS snippet, "
        "bm25(memory_fts) AS rank "
        "FROM memory_fts WHERE memory_fts MATCH ?"
    )
    params: list[Any] = [match_query]
    if source:
        sql += " AND source = ?"
        params.append(source)
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)

    cursor = await conn.execute(sql, params)
    rows = await cursor.fetchall()
    return [
        {
            "source": row["source"],
            "source_id": row["source_id"],
            "title": row["title"],
            "snippet": row["snippet"],
        }
        for row in rows
    ]
