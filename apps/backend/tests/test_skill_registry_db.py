from __future__ import annotations

import time

import aiosqlite
import pytest

from skill_registry import db as registry_db
from storage.migrator import run_migrations


@pytest.fixture
async def conn(tmp_path):
    db_path = tmp_path / "skill_registry_test.db"
    run_migrations(db_path)
    connection = await aiosqlite.connect(str(db_path))
    connection.row_factory = aiosqlite.Row
    yield connection
    await connection.close()


def _record(i: int, source: str = "official", tags: list[str] | None = None) -> dict:
    return {
        "identifier": f"src/{source}/skill-{i}",
        "name": f"Skill {i}",
        "description": f"Description for skill {i} covering python automation topic {i % 7}",
        "source": source,
        "trust_level": "builtin" if i % 5 == 0 else "community",
        "repo": "org/repo",
        "path": f"skills/skill-{i}",
        "tags": tags or ["python", "automation"],
        "extra": {},
    }


async def test_upsert_inserts_new_records_and_reports_stats(conn):
    records = [_record(i) for i in range(10)]
    stats = await registry_db.upsert_catalog_records(conn, records, catalog_version=1)
    assert stats.inserted == 10
    assert stats.updated == 0
    assert stats.unchanged == 0

    cursor = await conn.execute("SELECT COUNT(*) AS n FROM skill_catalog")
    assert (await cursor.fetchone())["n"] == 10


async def test_upsert_deduplicates_by_identifier(conn):
    records = [_record(0), _record(0)]  # identical duplicate identifier in one batch
    stats = await registry_db.upsert_catalog_records(conn, records, catalog_version=1)
    cursor = await conn.execute("SELECT COUNT(*) AS n FROM skill_catalog")
    assert (await cursor.fetchone())["n"] == 1
    assert stats.inserted <= 2  # SQLite upsert collapses the duplicate row


async def test_upsert_detects_unchanged_vs_updated_records(conn):
    records = [_record(0)]
    await registry_db.upsert_catalog_records(conn, records, catalog_version=1)

    # Re-sync with identical content -> unchanged.
    stats = await registry_db.upsert_catalog_records(conn, records, catalog_version=1)
    assert stats.unchanged == 1
    assert stats.updated == 0

    # Re-sync with a changed description -> updated.
    changed = [dict(records[0], description="a brand new description")]
    stats = await registry_db.upsert_catalog_records(conn, changed, catalog_version=1)
    assert stats.updated == 1
    assert stats.unchanged == 0


async def test_upsert_preserves_source_field(conn):
    records = [_record(0, source="clawhub"), _record(1, source="github")]
    await registry_db.upsert_catalog_records(conn, records, catalog_version=1)

    clawhub = await registry_db.get_skill(conn, records[0]["identifier"])
    github = await registry_db.get_skill(conn, records[1]["identifier"])
    assert clawhub["source"] == "clawhub"
    assert github["source"] == "github"


async def test_fts_search_finds_by_name_and_description(conn):
    records = [_record(i) for i in range(20)]
    records.append(_record(999, source="github", tags=["kubernetes", "devops"]))
    records[-1]["name"] = "Kubernetes Operator Helper"
    records[-1]["description"] = "Manage Kubernetes clusters and operators."
    await registry_db.upsert_catalog_records(conn, records, catalog_version=1)

    results = await registry_db.search_catalog(conn, "kubernetes")
    assert any(r["identifier"] == "src/github/skill-999" for r in results)


async def test_search_ranks_exact_identifier_match_first(conn):
    records = [_record(i) for i in range(5)]
    await registry_db.upsert_catalog_records(conn, records, catalog_version=1)

    results = await registry_db.search_catalog(conn, "src/official/skill-3")
    assert results[0]["identifier"] == "src/official/skill-3"


async def test_search_prefers_trusted_sources(conn):
    trusted = _record(0, source="official")
    trusted["trust_level"] = "builtin"
    trusted["name"] = "Automation Helper Trusted"
    community = _record(1, source="clawhub")
    community["trust_level"] = "community"
    community["name"] = "Automation Helper Community"
    await registry_db.upsert_catalog_records(conn, [trusted, community], catalog_version=1)

    results = await registry_db.search_catalog(conn, "automation helper")
    trusted_idx = next(i for i, r in enumerate(results) if r["identifier"] == trusted["identifier"])
    community_idx = next(i for i, r in enumerate(results) if r["identifier"] == community["identifier"])
    assert trusted_idx < community_idx


async def test_5000_plus_catalog_records_index_and_search_within_reasonable_time(conn):
    records = [_record(i, source=["official", "skills.sh", "clawhub", "github"][i % 4]) for i in range(5500)]
    t0 = time.monotonic()
    stats = await registry_db.upsert_catalog_records(conn, records, catalog_version=1)
    index_seconds = time.monotonic() - t0
    assert stats.inserted == 5500
    assert index_seconds < 30, f"indexing 5500 records took {index_seconds:.2f}s"

    t0 = time.monotonic()
    results = await registry_db.search_catalog(conn, "python automation", limit=25)
    search_seconds = time.monotonic() - t0
    assert len(results) > 0
    assert search_seconds < 2, f"search took {search_seconds:.2f}s"


async def test_sync_log_lifecycle(conn):
    sync_id = await registry_db.start_sync_log(conn, "https://example.test/catalog.json", "hosted_primary")
    await registry_db.finish_sync_log(
        conn, sync_id, status="SUCCEEDED", record_count=100, raw_size_bytes=1000,
        compressed_size_bytes=200, sha256="abc123", duration_seconds=1.5,
    )
    last = await registry_db.get_last_sync(conn)
    assert last["sync_id"] == sync_id
    assert last["record_count"] == 100
    assert last["status"] == "SUCCEEDED"


async def test_installed_lifecycle_install_update_uninstall(conn):
    await registry_db.upsert_installed(
        conn, identifier="official/example", name="Example", category="Coding",
        local_path="Coding/example", source="official", trust_level="builtin",
        content_hash="hash1", action="install",
    )
    installed = await registry_db.get_installed(conn, "official/example")
    assert installed["status"] == "installed"
    assert installed["content_hash"] == "hash1"

    await registry_db.upsert_installed(
        conn, identifier="official/example", name="Example", category="Coding",
        local_path="Coding/example", source="official", trust_level="builtin",
        content_hash="hash2", action="update",
    )
    updated = await registry_db.get_installed(conn, "official/example")
    assert updated["content_hash"] == "hash2"

    versions_cursor = await conn.execute("SELECT action FROM skill_versions WHERE identifier = ? ORDER BY created_at", ("official/example",))
    actions = [r["action"] for r in await versions_cursor.fetchall()]
    assert actions == ["install", "update"]

    await registry_db.mark_uninstalled(conn, "official/example", "hash2")
    listed = await registry_db.list_installed(conn)
    assert listed == []
    gone = await registry_db.get_installed(conn, "official/example")
    assert gone["status"] == "uninstalled"


async def test_duplicate_install_is_idempotent_not_duplicated(conn):
    for _ in range(3):
        await registry_db.upsert_installed(
            conn, identifier="official/dup", name="Dup", category="Coding",
            local_path="Coding/dup", source="official", trust_level="builtin",
            content_hash="samehash", action="install",
        )
    cursor = await conn.execute("SELECT COUNT(*) AS n FROM installed_skills WHERE identifier = ?", ("official/dup",))
    assert (await cursor.fetchone())["n"] == 1


async def test_catalog_summary_reports_real_counts(conn):
    records = [_record(i, source="official" if i < 3 else "clawhub") for i in range(10)]
    for r in records[:3]:
        r["trust_level"] = "builtin"
    for r in records[3:]:
        r["trust_level"] = "community"
    await registry_db.upsert_catalog_records(conn, records, catalog_version=1)
    await registry_db.upsert_installed(
        conn, identifier=records[0]["identifier"], name="X", category="Coding",
        local_path="Coding/x", source="official", trust_level="builtin",
        content_hash="h", action="install",
    )

    summary = await registry_db.catalog_summary(conn)
    assert summary["total_records"] == 10
    assert summary["trusted_records"] == 3
    assert summary["community_records"] == 7
    assert summary["installed_count"] == 1


async def test_audit_record_roundtrip(conn):
    await registry_db.record_audit(conn, "official/example", passed=False, findings=["missing SKILL.md"], quarantine_path="/tmp/x")
    cursor = await conn.execute("SELECT * FROM skill_audit WHERE identifier = ?", ("official/example",))
    row = await cursor.fetchone()
    assert row["passed"] == 0
    import json

    assert json.loads(row["findings"]) == ["missing SKILL.md"]
