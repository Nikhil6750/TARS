from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

import skill_registry.manager as manager_module
from skill_registry.installer import DownloadResult
from skill_registry.manager import SkillManager
from storage.migrator import run_migrations

_RECORD = {
    "identifier": "official/example-skill",
    "name": "Example Skill",
    "description": "A test skill for the registry.",
    "source": "official",
    "trust_level": "builtin",
    "repo": "org/repo",
    "path": "skills/example",
    "tags": ["python", "testing"],
    "extra": {},
}


@pytest.fixture
async def conn(tmp_path):
    db_path = tmp_path / "manager_test.db"
    run_migrations(db_path)
    connection = await aiosqlite.connect(str(db_path))
    connection.row_factory = aiosqlite.Row
    yield connection
    await connection.close()


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "Obsidian Vault"
    v.mkdir()
    (v / ".obsidian").mkdir()
    return v


@pytest.fixture
async def manager(conn, vault, tmp_path):
    from skill_registry import db as registry_db

    await registry_db.upsert_catalog_records(conn, [_RECORD], catalog_version=1)
    return SkillManager(conn, str(vault), tmp_path / "catalog.json.gz")


def _fake_download_factory(quarantine_content_root: Path):
    async def _fake_download(record, quarantine_root, timeout_seconds=30.0):
        dest = quarantine_root / "fake-download"
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "SKILL.md").write_text(
            f"---\nname: {record['name']}\n---\n\n# {record['name']}\n", encoding="utf-8"
        )
        return DownloadResult(quarantine_path=dest, file_count=1)

    return _fake_download


async def test_install_skill_creates_bundle_inside_vault_only(manager, vault, monkeypatch):
    monkeypatch.setattr(manager_module, "download_to_quarantine", _fake_download_factory(vault))

    result = await manager.install_skill(_RECORD["identifier"])
    assert result.installed is True
    assert result.local_path is not None

    bundle_path = vault / result.local_path
    assert bundle_path.is_dir()
    assert (bundle_path / "SKILL.md").is_file()
    # Must live under TARS/Skills -- never at the vault root or elsewhere.
    assert "TARS" in bundle_path.parts
    assert "Skills" in bundle_path.parts


async def test_install_skill_marks_installed_in_registry(manager, monkeypatch, vault):
    monkeypatch.setattr(manager_module, "download_to_quarantine", _fake_download_factory(vault))
    await manager.install_skill(_RECORD["identifier"])

    installed = await manager.list_installed()
    assert len(installed) == 1
    assert installed[0]["identifier"] == _RECORD["identifier"]


async def test_install_skill_updates_installed_skills_obsidian_note(manager, monkeypatch, vault):
    monkeypatch.setattr(manager_module, "download_to_quarantine", _fake_download_factory(vault))
    await manager.install_skill(_RECORD["identifier"])

    note_path = vault / "TARS" / "Skills" / "_Registry" / "Installed Skills.md"
    assert note_path.is_file()
    content = note_path.read_text(encoding="utf-8")
    assert "Example Skill" in content
    assert "official/example-skill" in content


async def test_uninstall_skill_removes_bundle_and_updates_registry(manager, monkeypatch, vault):
    monkeypatch.setattr(manager_module, "download_to_quarantine", _fake_download_factory(vault))
    result = await manager.install_skill(_RECORD["identifier"])
    bundle_path = vault / result.local_path
    assert bundle_path.is_dir()

    ok = await manager.uninstall_skill(_RECORD["identifier"])
    assert ok is True
    assert not bundle_path.exists()
    assert await manager.list_installed() == []

    note_path = vault / "TARS" / "Skills" / "_Registry" / "Installed Skills.md"
    content = note_path.read_text(encoding="utf-8")
    assert "No skills installed yet" in content


async def test_uninstall_nonexistent_skill_returns_false(manager):
    ok = await manager.uninstall_skill("does/not-exist")
    assert ok is False


async def test_duplicate_installation_does_not_create_two_bundles(manager, monkeypatch, vault):
    monkeypatch.setattr(manager_module, "download_to_quarantine", _fake_download_factory(vault))
    r1 = await manager.install_skill(_RECORD["identifier"])
    r2 = await manager.install_skill(_RECORD["identifier"])
    assert r1.local_path == r2.local_path
    installed = await manager.list_installed()
    assert len(installed) == 1


async def test_reindex_installed_marks_manually_deleted_bundle_uninstalled(manager, monkeypatch, vault):
    monkeypatch.setattr(manager_module, "download_to_quarantine", _fake_download_factory(vault))
    result = await manager.install_skill(_RECORD["identifier"])
    bundle_path = vault / result.local_path
    import shutil

    shutil.rmtree(bundle_path)  # simulate a manual deletion inside the vault

    changed = await manager.reindex_installed()
    assert changed == 1
    installed = await manager.list_installed()
    assert installed == []


async def test_search_skills_returns_metadata_only_not_full_bundle_content(manager):
    # Progressive disclosure (Phase 10): search results must never contain
    # SKILL.md body content, only catalog metadata -- full content is only
    # loaded by install_skill()/inspect_skill() for a specific identifier.
    results = await manager.search_skills("Example Skill")
    assert len(results) >= 1
    for r in results:
        assert "content" not in r
        assert "body" not in r
        assert set(r.keys()) <= {"identifier", "name", "description", "source", "trust_level", "tags"}


async def test_ensure_vault_structure_never_touches_unrelated_vault_files(manager, vault):
    (vault / "Welcome.md").write_text("pre-existing personal note", encoding="utf-8")
    await manager.sync_obsidian_registry()

    assert (vault / "Welcome.md").read_text(encoding="utf-8") == "pre-existing personal note"
    assert (vault / "TARS" / "Skills" / "_Registry").is_dir()


async def test_vault_structure_uses_windows_style_paths_correctly(manager, vault):
    await manager.sync_obsidian_registry()
    skills_root = vault / "TARS" / "Skills"
    assert skills_root.is_dir()
    from skill_registry.categorize import CATEGORIES

    for category in CATEGORIES:
        assert (skills_root / category).is_dir()
