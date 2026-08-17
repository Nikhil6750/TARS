from __future__ import annotations

import aiosqlite
import pytest

from app.action_contracts import (
    ActionRequest,
    ActionSource,
    ActionStatus,
    RiskLevel,
    SkillValidationError,
)
from memory.service import MemoryService
from skills.obsidian import ObsidianSkill
from storage.migrator import run_migrations


@pytest.fixture
async def conn(tmp_path):
    db_path = tmp_path / "obsidian_skill_test.db"
    run_migrations(db_path)
    connection = await aiosqlite.connect(str(db_path))
    connection.row_factory = aiosqlite.Row
    yield connection
    await connection.close()


@pytest.fixture
async def vault(tmp_path):
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    (vault_dir / "risk.md").write_text(
        "# Risk Management\n\nNever risk more than one percent per trade.",
        encoding="utf-8",
    )
    return vault_dir


@pytest.fixture
async def skill(conn, vault):
    memory = MemoryService(conn, vault_path=str(vault), sqlite_vec_enabled=False)
    await memory.reindex_vault()
    return ObsidianSkill(memory, str(vault))


def _request(action: str, arguments: dict) -> ActionRequest:
    return ActionRequest(
        skill="obsidian", action=action, arguments=arguments, source=ActionSource.hud
    )


def test_classify_risk(skill):
    assert skill.classify_risk("search", {}) == RiskLevel.READ_ONLY
    assert skill.classify_risk("read", {}) == RiskLevel.READ_ONLY
    assert skill.classify_risk("write", {}) == RiskLevel.BLOCKED


async def test_validate_search_requires_query(skill):
    with pytest.raises(SkillValidationError):
        await skill.validate("search", {})
    await skill.validate("search", {"query": "risk"})


async def test_validate_read_requires_path(skill):
    with pytest.raises(SkillValidationError):
        await skill.validate("read", {})


async def test_validate_read_rejects_path_escaping_vault(skill):
    with pytest.raises(SkillValidationError):
        await skill.validate("read", {"path": "..\\..\\Windows\\System32\\config"})


async def test_validate_rejects_unknown_action(skill):
    with pytest.raises(SkillValidationError):
        await skill.validate("delete", {})


async def test_execute_search_delegates_to_memory_service_and_preserves_provenance(skill):
    result = await skill.execute(_request("search", {"query": "risk"}))

    assert result.status == ActionStatus.SUCCEEDED
    assert len(result.data["results"]) == 1
    hit = result.data["results"][0]
    assert hit["source"] == "vault"
    assert hit["source_id"] == "risk.md"


async def test_execute_search_returns_empty_for_no_match(skill):
    result = await skill.execute(_request("search", {"query": "nonexistentword"}))
    assert result.status == ActionStatus.SUCCEEDED
    assert result.data["results"] == []


async def test_execute_read_returns_real_file_content(skill):
    result = await skill.execute(_request("read", {"path": "risk.md"}))

    assert result.status == ActionStatus.SUCCEEDED
    assert "one percent per trade" in result.data["content"]
    assert result.data["source"] == "vault"
    assert result.data["source_id"] == "risk.md"


async def test_execute_read_reports_failure_for_missing_note(skill):
    result = await skill.execute(_request("read", {"path": "does-not-exist.md"}))
    assert result.status == ActionStatus.FAILED
    assert result.error is not None
