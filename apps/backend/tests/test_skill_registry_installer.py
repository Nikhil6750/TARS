from __future__ import annotations

import pytest

from skill_registry.categorize import categorize, safe_folder_slug
from skill_registry.installer import (
    UnresolvableSourceError,
    download_to_quarantine,
    promote_to_vault,
)
from skill_registry.validation import compute_bundle_hash, validate_quarantined_bundle


def _write_bundle(root, frontmatter_name="example"):
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(
        f"---\nname: {frontmatter_name}\ndescription: test skill\n---\n\n# {frontmatter_name}\n", encoding="utf-8"
    )


def test_validate_quarantined_bundle_accepts_well_formed_bundle(tmp_path):
    bundle = tmp_path / "bundle"
    _write_bundle(bundle)
    result = validate_quarantined_bundle(bundle)
    assert result.passed is True
    assert result.findings == []


def test_validate_quarantined_bundle_rejects_missing_skill_md(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "readme.md").write_text("no skill here", encoding="utf-8")
    result = validate_quarantined_bundle(bundle)
    assert result.passed is False
    assert any("SKILL.md not found" in f for f in result.findings)


def test_validate_quarantined_bundle_rejects_missing_frontmatter(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "SKILL.md").write_text("# just a heading, no frontmatter", encoding="utf-8")
    result = validate_quarantined_bundle(bundle)
    assert result.passed is False
    assert any("frontmatter" in f for f in result.findings)


def test_validate_quarantined_bundle_rejects_oversized_bundle(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    _write_bundle(bundle)
    (bundle / "big.bin").write_bytes(b"x" * 1000)

    import skill_registry.validation as validation_module

    monkeypatch.setattr(validation_module, "MAX_BUNDLE_SIZE_BYTES", 500)
    result = validation_module.validate_quarantined_bundle(bundle)
    assert result.passed is False
    assert any("exceeds limit" in f for f in result.findings)


def test_validate_quarantined_bundle_detects_symlink_escape(tmp_path):
    bundle = tmp_path / "bundle"
    _write_bundle(bundle)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = bundle / "escape.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation requires elevated privileges on this system")

    result = validate_quarantined_bundle(bundle)
    assert result.passed is False
    assert any("escapes bundle root" in f for f in result.findings)


def test_compute_bundle_hash_is_deterministic_and_content_sensitive(tmp_path):
    bundle = tmp_path / "bundle"
    _write_bundle(bundle)
    h1 = compute_bundle_hash(bundle)
    h2 = compute_bundle_hash(bundle)
    assert h1 == h2

    (bundle / "SKILL.md").write_text("---\nname: changed\n---\n", encoding="utf-8")
    h3 = compute_bundle_hash(bundle)
    assert h3 != h1


async def test_download_to_quarantine_rejects_unresolvable_source(tmp_path):
    record = {"identifier": "clawhub/no-repo", "source": "clawhub", "repo": "", "path": ""}
    with pytest.raises(UnresolvableSourceError):
        await download_to_quarantine(record, tmp_path)


def test_promote_to_vault_moves_bundle_into_category_dir(tmp_path):
    quarantine = tmp_path / "quarantine" / "abc123"
    _write_bundle(quarantine)
    dest = tmp_path / "vault" / "TARS" / "Skills" / "Coding" / "example"

    promote_to_vault(quarantine, dest)

    assert not quarantine.exists()
    assert (dest / "SKILL.md").is_file()


def test_categorize_uses_tags_to_pick_a_fixed_category():
    record = {"name": "kubectl-helper", "description": "Manage k8s clusters", "tags": ["kubernetes", "devops"]}
    assert categorize(record) == "Automation"

    record2 = {"name": "trading-journal", "description": "Log trades and PnL", "tags": ["trading", "finance"]}
    assert categorize(record2) == "Trading"


def test_categorize_falls_back_to_other_for_unknown_tags():
    record = {"name": "mystery-skill", "description": "does something unusual", "tags": ["xyzzy"]}
    assert categorize(record) == "Other"


def test_safe_folder_slug_strips_reserved_windows_characters():
    assert safe_folder_slug('bad<>:"/\\|?*name') == "bad-name"


def test_safe_folder_slug_handles_empty_and_dot_only_input():
    assert safe_folder_slug("") == "unnamed-skill"
    assert safe_folder_slug("...") == "unnamed-skill"


def test_safe_folder_slug_collapses_whitespace():
    assert safe_folder_slug("  My   Cool   Skill  ") == "My-Cool-Skill"
