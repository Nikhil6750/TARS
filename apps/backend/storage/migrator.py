"""Minimal, dependency-free SQLite migration runner.

Migrations are plain `.sql` files in `storage/migrations/`, named
`NNNN_description.sql` and applied in filename order exactly once, tracked
in a `schema_migrations` table. No ORM — the schema is small and explicit
per ARCHITECTURE.md's "SQLite for lightweight TARS state/history" scope.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def _ensure_tracking_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename    TEXT PRIMARY KEY,
            applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """
    )
    conn.commit()


def applied_migrations(conn: sqlite3.Connection) -> set[str]:
    _ensure_tracking_table(conn)
    rows = conn.execute("SELECT filename FROM schema_migrations").fetchall()
    return {row[0] for row in rows}


def pending_migrations() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def run_migrations(db_path: Path) -> list[str]:
    """Apply any migration files not yet recorded as applied. Returns the
    list of filenames applied during this call (empty if already current)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        already = applied_migrations(conn)
        newly_applied: list[str] = []
        for path in pending_migrations():
            if path.name in already:
                continue
            sql = path.read_text(encoding="utf-8")
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations (filename) VALUES (?)", (path.name,)
            )
            conn.commit()
            newly_applied.append(path.name)
        return newly_applied
    finally:
        conn.close()
