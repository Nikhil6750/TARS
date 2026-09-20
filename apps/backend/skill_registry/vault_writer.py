"""Creates/writes the TARS/Skills subtree inside the configured Obsidian
vault (Phase 4). Only ever touches paths under `<vault>/TARS/Skills/` --
never the rest of the vault (Phase 1's "never destroy or reorganize
unrelated Obsidian notes").
"""
from __future__ import annotations

from pathlib import Path

from skill_registry.categorize import CATEGORIES

_SKILLS_SUBPATH = ("TARS", "Skills")


def skills_root(vault_path: str) -> Path:
    return Path(vault_path).joinpath(*_SKILLS_SUBPATH)


def ensure_vault_structure(vault_path: str) -> list[str]:
    """Idempotently creates the TARS/Skills tree (category folders +
    _Registry + _Manifests + _Quarantine). Returns the list of directories
    actually created (empty if everything already existed)."""
    root = skills_root(vault_path)
    created: list[str] = []

    def _mkdir(p: Path) -> None:
        if not p.is_dir():
            p.mkdir(parents=True, exist_ok=True)
            created.append(str(p))

    _mkdir(root)
    for category in CATEGORIES:
        _mkdir(root / category)
    _mkdir(root / "_Registry")
    _mkdir(root / "_Manifests")
    _mkdir(root / "_Quarantine")
    return created


def registry_note_path(vault_path: str, filename: str) -> Path:
    return skills_root(vault_path) / "_Registry" / filename


def manifest_path(vault_path: str, filename: str) -> Path:
    return skills_root(vault_path) / "_Manifests" / filename


def quarantine_root(vault_path: str) -> Path:
    return skills_root(vault_path) / "_Quarantine"


def category_dir(vault_path: str, category: str) -> Path:
    if category not in CATEGORIES:
        category = "Other"
    return skills_root(vault_path) / category


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
