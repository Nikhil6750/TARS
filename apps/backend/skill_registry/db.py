"""SQLite access for the skill catalog + installed-skills tables (see
storage/migrations/0003_skill_registry.sql). Batches writes for 90k-scale
catalog syncs -- one commit per batch, not per row, matching the general
convention (memory/vault.py) but tuned for volume.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import aiosqlite

from skill_registry.catalog import record_content_hash

_BATCH_SIZE = 2000


@dataclass
class SyncStats:
    inserted: int
    updated: int
    unchanged: int
    sources: dict[str, int]


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def upsert_catalog_records(
    conn: aiosqlite.Connection, records: list[dict[str, Any]], catalog_version: int
) -> SyncStats:
    now = _now()
    cursor = await conn.execute("SELECT identifier, content_hash FROM skill_catalog")
    existing = {row["identifier"]: row["content_hash"] for row in await cursor.fetchall()}

    inserted = updated = unchanged = 0
    sources: dict[str, int] = {}
    fts_rows: list[tuple[str, str, str, str, str]] = []
    catalog_rows: list[tuple] = []

    for record in records:
        identifier = record["identifier"]
        content_hash = record_content_hash(record)
        source = str(record.get("source", "unknown"))
        sources[source] = sources.get(source, 0) + 1

        prior_hash = existing.get(identifier)
        if prior_hash == content_hash:
            unchanged += 1
            continue
        if prior_hash is not None:
            updated += 1
        else:
            inserted += 1

        tags = record.get("tags") or []
        platform = record.get("platform") or []
        extra = record.get("extra") or {}

        catalog_rows.append(
            (
                identifier,
                str(record.get("name", "")),
                str(record.get("description", "")),
                source,
                str(record.get("trust_level", "community")),
                record.get("repo"),
                record.get("path"),
                json.dumps(tags),
                json.dumps(platform),
                json.dumps(extra),
                content_hash,
                catalog_version,
                now,
                now,
            )
        )
        fts_rows.append(
            (
                identifier,
                str(record.get("name", "")),
                str(record.get("description", "")),
                " ".join(str(t) for t in tags),
                source,
            )
        )

    for i in range(0, len(catalog_rows), _BATCH_SIZE):
        batch = catalog_rows[i : i + _BATCH_SIZE]
        await conn.executemany(
            """
            INSERT INTO skill_catalog
                (identifier, name, description, source, trust_level, repo, path,
                 tags, platform, extra, content_hash, catalog_version, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(identifier) DO UPDATE SET
                name = excluded.name,
                description = excluded.description,
                source = excluded.source,
                trust_level = excluded.trust_level,
                repo = excluded.repo,
                path = excluded.path,
                tags = excluded.tags,
                platform = excluded.platform,
                extra = excluded.extra,
                content_hash = excluded.content_hash,
                catalog_version = excluded.catalog_version,
                last_seen_at = excluded.last_seen_at
            """,
            batch,
        )
        await conn.commit()

    # last_seen_at must also advance for records that were unchanged this
    # sync -- otherwise a stale-record sweep would wrongly think they
    # vanished from the catalog.
    if records:
        seen_ids = [r["identifier"] for r in records]
        for i in range(0, len(seen_ids), _BATCH_SIZE):
            batch_ids = seen_ids[i : i + _BATCH_SIZE]
            placeholders = ",".join("?" for _ in batch_ids)
            await conn.execute(
                f"UPDATE skill_catalog SET last_seen_at = ? WHERE identifier IN ({placeholders})",
                [now, *batch_ids],
            )
        await conn.commit()

    for i in range(0, len(fts_rows), _BATCH_SIZE):
        batch = fts_rows[i : i + _BATCH_SIZE]
        ids = [row[0] for row in batch]
        placeholders = ",".join("?" for _ in ids)
        await conn.execute(f"DELETE FROM skill_catalog_fts WHERE identifier IN ({placeholders})", ids)
        await conn.executemany(
            "INSERT INTO skill_catalog_fts (identifier, name, description, tags, source) VALUES (?, ?, ?, ?, ?)",
            batch,
        )
        await conn.commit()

    for source, count in sources.items():
        await conn.execute(
            """
            INSERT INTO skill_sources (source, record_count, last_synced_at)
            VALUES (?, ?, ?)
            ON CONFLICT(source) DO UPDATE SET record_count = excluded.record_count, last_synced_at = excluded.last_synced_at
            """,
            (source, count, now),
        )
    await conn.commit()

    return SyncStats(inserted=inserted, updated=updated, unchanged=unchanged, sources=sources)


async def start_sync_log(conn: aiosqlite.Connection, catalog_url: str, acquisition_method: str) -> str:
    sync_id = str(uuid4())
    await conn.execute(
        """
        INSERT INTO skill_sync_log (sync_id, started_at, catalog_url, acquisition_method, status)
        VALUES (?, ?, ?, ?, 'RUNNING')
        """,
        (sync_id, _now(), catalog_url, acquisition_method),
    )
    await conn.commit()
    return sync_id


async def finish_sync_log(
    conn: aiosqlite.Connection,
    sync_id: str,
    *,
    status: str,
    record_count: int | None = None,
    raw_size_bytes: int | None = None,
    compressed_size_bytes: int | None = None,
    sha256: str | None = None,
    duration_seconds: float | None = None,
    error: str | None = None,
) -> None:
    await conn.execute(
        """
        UPDATE skill_sync_log SET
            finished_at = ?, status = ?, record_count = ?, raw_size_bytes = ?,
            compressed_size_bytes = ?, sha256 = ?, duration_seconds = ?, error = ?
        WHERE sync_id = ?
        """,
        (_now(), status, record_count, raw_size_bytes, compressed_size_bytes, sha256, duration_seconds, error, sync_id),
    )
    await conn.commit()


async def get_last_sync(conn: aiosqlite.Connection) -> dict[str, Any] | None:
    cursor = await conn.execute(
        "SELECT * FROM skill_sync_log WHERE status = 'SUCCEEDED' ORDER BY finished_at DESC LIMIT 1"
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_skill(conn: aiosqlite.Connection, identifier: str) -> dict[str, Any] | None:
    cursor = await conn.execute("SELECT * FROM skill_catalog WHERE identifier = ?", (identifier,))
    row = await cursor.fetchone()
    if row is None:
        return None
    result = dict(row)
    result["tags"] = json.loads(result["tags"])
    result["platform"] = json.loads(result["platform"])
    result["extra"] = json.loads(result["extra"])
    return result


async def search_catalog(
    conn: aiosqlite.Connection, query: str, limit: int = 20, installed_identifiers: set[str] | None = None
) -> list[dict[str, Any]]:
    """FTS5 search ranked per Phase 7: exact identifier/name match first,
    then FTS relevance, then trusted source, then already-installed."""
    from memory.fts import build_match_query

    installed_identifiers = installed_identifiers or set()
    query_stripped = query.strip()
    if not query_stripped:
        return []

    # Exact identifier/name matches, regardless of FTS tokenization.
    exact_cursor = await conn.execute(
        "SELECT identifier, name, description, source, trust_level, tags "
        "FROM skill_catalog WHERE identifier = ? OR lower(name) = lower(?) LIMIT ?",
        (query_stripped, query_stripped, limit),
    )
    exact_rows = [dict(r) for r in await exact_cursor.fetchall()]
    exact_ids = {r["identifier"] for r in exact_rows}

    match_query = build_match_query(query_stripped)
    fts_rows: list[dict[str, Any]] = []
    if match_query and len(exact_rows) < limit:
        fts_cursor = await conn.execute(
            """
            SELECT c.identifier, c.name, c.description, c.source, c.trust_level, c.tags,
                   bm25(skill_catalog_fts) AS rank
            FROM skill_catalog_fts
            JOIN skill_catalog c ON c.identifier = skill_catalog_fts.identifier
            WHERE skill_catalog_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (match_query, limit * 3),
        )
        fts_rows = [dict(r) for r in await fts_cursor.fetchall() if r["identifier"] not in exact_ids]

    def sort_key(row: dict[str, Any]) -> tuple:
        trusted = row.get("trust_level") in ("builtin", "official", "verified")
        already_installed = row["identifier"] in installed_identifiers
        return (0 if trusted else 1, 0 if already_installed else 1, row.get("rank", 0))

    fts_rows.sort(key=sort_key)
    combined = exact_rows + fts_rows
    for row in combined:
        row["tags"] = json.loads(row["tags"]) if isinstance(row.get("tags"), str) else row.get("tags", [])
        row.pop("rank", None)
    return combined[:limit]


async def list_installed(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    cursor = await conn.execute(
        "SELECT * FROM installed_skills WHERE status = 'installed' ORDER BY category, name"
    )
    return [dict(r) for r in await cursor.fetchall()]


async def get_installed(conn: aiosqlite.Connection, identifier: str) -> dict[str, Any] | None:
    cursor = await conn.execute("SELECT * FROM installed_skills WHERE identifier = ?", (identifier,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def upsert_installed(
    conn: aiosqlite.Connection,
    *,
    identifier: str,
    name: str,
    category: str,
    local_path: str,
    source: str | None,
    trust_level: str | None,
    content_hash: str,
    action: str,
    notes: str | None = None,
) -> None:
    now = _now()
    existing = await get_installed(conn, identifier)
    if existing is None:
        await conn.execute(
            """
            INSERT INTO installed_skills
                (identifier, name, category, local_path, source, trust_level, content_hash, status, installed_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'installed', ?, ?)
            """,
            (identifier, name, category, local_path, source, trust_level, content_hash, now, now),
        )
    else:
        await conn.execute(
            """
            UPDATE installed_skills SET
                name = ?, category = ?, local_path = ?, source = ?, trust_level = ?,
                content_hash = ?, status = 'installed', updated_at = ?
            WHERE identifier = ?
            """,
            (name, category, local_path, source, trust_level, content_hash, now, identifier),
        )
    await conn.execute(
        "INSERT INTO skill_versions (version_id, identifier, content_hash, action, notes, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (str(uuid4()), identifier, content_hash, action, notes, now),
    )
    await conn.commit()


async def mark_uninstalled(conn: aiosqlite.Connection, identifier: str, content_hash: str) -> None:
    now = _now()
    await conn.execute(
        "UPDATE installed_skills SET status = 'uninstalled', updated_at = ? WHERE identifier = ?",
        (now, identifier),
    )
    await conn.execute(
        "INSERT INTO skill_versions (version_id, identifier, content_hash, action, notes, created_at) VALUES (?, ?, ?, 'uninstall', NULL, ?)",
        (str(uuid4()), identifier, content_hash, now),
    )
    await conn.commit()


async def record_audit(
    conn: aiosqlite.Connection, identifier: str, passed: bool, findings: list[str], quarantine_path: str | None
) -> None:
    await conn.execute(
        """
        INSERT INTO skill_audit (audit_id, identifier, checked_at, passed, findings, quarantine_path)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (str(uuid4()), identifier, _now(), 1 if passed else 0, json.dumps(findings), quarantine_path),
    )
    await conn.commit()


async def catalog_summary(conn: aiosqlite.Connection) -> dict[str, Any]:
    total_cursor = await conn.execute("SELECT COUNT(*) AS n FROM skill_catalog")
    total = (await total_cursor.fetchone())["n"]
    trusted_cursor = await conn.execute(
        "SELECT COUNT(*) AS n FROM skill_catalog WHERE trust_level IN ('builtin','official','verified')"
    )
    trusted = (await trusted_cursor.fetchone())["n"]
    installed_cursor = await conn.execute("SELECT COUNT(*) AS n FROM installed_skills WHERE status = 'installed'")
    installed = (await installed_cursor.fetchone())["n"]
    sources_cursor = await conn.execute("SELECT source, record_count, last_synced_at FROM skill_sources ORDER BY record_count DESC")
    sources = [dict(r) for r in await sources_cursor.fetchall()]
    return {
        "total_records": total,
        "trusted_records": trusted,
        "community_records": total - trusted,
        "installed_count": installed,
        "sources": sources,
    }


async def record_invocation(
    conn: aiosqlite.Connection, identifier: str, content_hash: str | None, user_task: str, result_status: str
) -> None:
    await conn.execute(
        """
        INSERT INTO skill_invocations (invocation_id, identifier, content_hash, invoked_at, user_task, result_status)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (str(uuid4()), identifier, content_hash, _now(), user_task, result_status),
    )
    await conn.commit()


async def list_invocations(conn: aiosqlite.Connection, identifier: str) -> list[dict[str, Any]]:
    cursor = await conn.execute(
        "SELECT * FROM skill_invocations WHERE identifier = ? ORDER BY invoked_at DESC", (identifier,)
    )
    return [dict(r) for r in await cursor.fetchall()]
