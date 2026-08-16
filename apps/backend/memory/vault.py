"""Read-only indexer for an Obsidian-compatible Markdown vault, per
ARCHITECTURE.md § Memory architecture — "Research knowledge" layer. This
module only ever reads vault files; it never creates, edits, or deletes a
note. Indexing is idempotent and content-hash-gated so re-running it is
cheap when nothing changed.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from memory import fts

logger = logging.getLogger("tars.memory.vault")

# Directories Obsidian/editors use for their own bookkeeping — never
# content the user authored, so never indexed.
_EXCLUDED_DIR_NAMES = {".obsidian", ".git", ".trash", "node_modules"}


@dataclass
class VaultIndexResult:
    vault_path: str
    indexed: int
    unchanged: int
    removed: int
    vault_missing: bool = False


def _iter_markdown_files(vault_root: Path):
    for path in vault_root.rglob("*.md"):
        if any(part in _EXCLUDED_DIR_NAMES for part in path.parts):
            continue
        yield path


def _extract_title(content: str, fallback: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip() or fallback
    return fallback


async def reindex_vault(conn: aiosqlite.Connection, vault_path: str) -> VaultIndexResult:
    vault_root = Path(vault_path)
    if not vault_root.is_dir():
        logger.info("obsidian vault path does not exist, skipping index: %s", vault_root)
        return VaultIndexResult(vault_path=str(vault_root), indexed=0, unchanged=0, removed=0, vault_missing=True)

    cursor = await conn.execute("SELECT path, content_hash FROM vault_documents")
    existing = {row["path"]: row["content_hash"] for row in await cursor.fetchall()}

    seen_paths: set[str] = set()
    indexed = 0
    unchanged = 0

    for file_path in _iter_markdown_files(vault_root):
        rel_path = str(file_path.relative_to(vault_root)).replace("\\", "/")
        seen_paths.add(rel_path)

        content = file_path.read_text(encoding="utf-8", errors="replace")
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        if existing.get(rel_path) == content_hash:
            unchanged += 1
            continue

        title = _extract_title(content, fallback=file_path.stem)
        await fts.upsert(conn, source="vault", source_id=rel_path, title=title, body=content)
        now = datetime.now(UTC).isoformat()
        await conn.execute(
            """
            INSERT INTO vault_documents (path, title, content_hash, indexed_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                title = excluded.title,
                content_hash = excluded.content_hash,
                indexed_at = excluded.indexed_at
            """,
            (rel_path, title, content_hash, now),
        )
        await conn.commit()
        indexed += 1

    removed_paths = set(existing) - seen_paths
    for rel_path in removed_paths:
        await fts.delete(conn, source="vault", source_id=rel_path)
        await conn.execute("DELETE FROM vault_documents WHERE path = ?", (rel_path,))
    if removed_paths:
        await conn.commit()

    return VaultIndexResult(
        vault_path=str(vault_root),
        indexed=indexed,
        unchanged=unchanged,
        removed=len(removed_paths),
    )
