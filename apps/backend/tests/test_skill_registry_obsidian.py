from __future__ import annotations

from skill_registry import obsidian_notes, vault_writer
from skill_registry.categorize import CATEGORIES


def test_ensure_vault_structure_creates_category_folders_and_registry_dirs(tmp_path):
    vault = tmp_path / "MyVault"
    vault.mkdir()
    created = vault_writer.ensure_vault_structure(str(vault))
    assert len(created) > 0

    root = vault_writer.skills_root(str(vault))
    for category in CATEGORIES:
        assert (root / category).is_dir()
    assert (root / "_Registry").is_dir()
    assert (root / "_Manifests").is_dir()
    assert (root / "_Quarantine").is_dir()


def test_ensure_vault_structure_is_idempotent(tmp_path):
    vault = tmp_path / "MyVault"
    vault.mkdir()
    vault_writer.ensure_vault_structure(str(vault))
    second_pass = vault_writer.ensure_vault_structure(str(vault))
    assert second_pass == []  # nothing new to create


def test_skills_root_never_escapes_vault_even_with_relative_segments(tmp_path):
    vault = tmp_path / "MyVault"
    root = vault_writer.skills_root(str(vault))
    assert str(root).startswith(str(vault))
    assert root.parts[-2:] == ("TARS", "Skills")


def test_render_skill_catalog_note_includes_generated_banner_and_counts():
    summary = {
        "total_records": 90605,
        "trusted_records": 543,
        "community_records": 90062,
        "installed_count": 1,
        "sources": [{"source": "clawhub", "record_count": 69150, "last_synced_at": "2026-08-20T00:00:00Z"}],
    }
    note = obsidian_notes.render_skill_catalog_note(summary)
    assert "Auto-generated" in note
    assert "90605" in note
    assert "clawhub" in note
    assert "69150" in note


def test_render_installed_skills_note_groups_by_category():
    installed = [
        {
            "identifier": "official/a",
            "name": "Skill A",
            "category": "Coding",
            "description": "desc a",
            "source": "official",
            "trust_level": "builtin",
            "content_hash": "abcdef1234567890",
            "local_path": "Coding/a",
            "updated_at": "2026-08-20T00:00:00Z",
        },
        {
            "identifier": "official/b",
            "name": "Skill B",
            "category": "Trading",
            "description": "desc b",
            "source": "official",
            "trust_level": "builtin",
            "content_hash": "1234567890abcdef",
            "local_path": "Trading/b",
            "updated_at": "2026-08-20T00:00:00Z",
        },
    ]
    note = obsidian_notes.render_installed_skills_note(installed)
    assert "## Coding" in note
    assert "## Trading" in note
    assert "Skill A" in note
    assert "Skill B" in note
    assert note.index("## Coding") < note.index("## Trading")  # alphabetical category order


def test_render_installed_skills_note_handles_empty_state():
    note = obsidian_notes.render_installed_skills_note([])
    assert "No skills installed yet" in note


def test_render_sync_status_note_handles_no_prior_sync():
    note = obsidian_notes.render_sync_status_note(None)
    assert "No successful sync recorded yet" in note


def test_render_sync_status_note_shows_real_fields():
    last_sync = {
        "finished_at": "2026-08-20T05:00:00Z",
        "record_count": 90605,
        "acquisition_method": "hosted_primary",
        "catalog_url": "https://hermes-agent.nousresearch.com/docs/api/skills-index.json",
        "sha256": "ea0ca169",
        "duration_seconds": 12.3,
        "error": None,
    }
    note = obsidian_notes.render_sync_status_note(last_sync)
    assert "90605" in note
    assert "hosted_primary" in note
    assert "ea0ca169" in note
