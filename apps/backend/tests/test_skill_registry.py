from __future__ import annotations

from app.action_contracts import Skill
from skills.registry import SKILLS, build_registry

_DB_INDEPENDENT_SKILLS = {
    "windows_app",
    "filesystem",
    "browser",
    "terminal",
    "desktop_control",
    # "trading" is always registered too (Trading Intelligence foundation) --
    # it needs no live app.state dependency to construct, same as the other
    # five; any action needing an unwired MemoryService/ChartAnalysisService/
    # TradingContextBuilder/FrontendCommandBridge fails closed at execute()
    # instead, per skills/trading.py's module docstring.
    "trading",
    # "skills" (the Hermes catalog + Obsidian skill registry surface) is
    # the same pattern again -- always registered, fails closed at
    # execute() with no SkillManager wired (skills/skill_registry_skill.py).
    "skills",
}


def test_eager_skills_registry_has_the_five_db_independent_skills():
    assert set(SKILLS) == _DB_INDEPENDENT_SKILLS
    for name, instance in SKILLS.items():
        assert isinstance(instance, Skill)
        assert instance.name == name


def test_build_registry_without_memory_service_matches_eager_skills():
    registry = build_registry()
    assert set(registry) == _DB_INDEPENDENT_SKILLS


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
        assert set(registry) == _DB_INDEPENDENT_SKILLS | {"obsidian"}
        assert isinstance(registry["skills"], Skill)
        assert isinstance(registry["obsidian"], Skill)
    finally:
        await conn.close()
