from __future__ import annotations

from app.action_contracts import Skill
from skills.registry import SKILLS, build_registry


def test_eager_skills_registry_has_the_four_db_independent_skills():
    assert set(SKILLS) == {"windows_app", "filesystem", "browser", "terminal"}
    for name, instance in SKILLS.items():
        assert isinstance(instance, Skill)
        assert instance.name == name


def test_build_registry_without_memory_service_matches_eager_skills():
    registry = build_registry()
    assert set(registry) == {"windows_app", "filesystem", "browser", "terminal"}


async def test_build_registry_with_memory_service_includes_obsidian(tmp_path):
    import aiosqlite

    from memory.service import MemoryService
    from storage.migrator import run_migrations

    db_path = tmp_path / "registry_test.db"
    run_migrations(db_path)
    conn = await aiosqlite.connect(str(db_path))
    conn.row_factory = aiosqlite.Row
    try:
        memory = MemoryService(conn, vault_path=str(tmp_path / "vault"), sqlite_vec_enabled=False)
        registry = build_registry(memory_service=memory, vault_path=str(tmp_path / "vault"))
        assert set(registry) == {"windows_app", "filesystem", "browser", "terminal", "obsidian"}
        assert isinstance(registry["obsidian"], Skill)
    finally:
        await conn.close()
