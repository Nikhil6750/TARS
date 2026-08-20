"""Quarantine validation for installed skill bundles (Phase 8/12). A
downloaded bundle is never promoted to the live Obsidian Skills directory
until every check here passes -- and nothing here ever executes a script
from the bundle; this module only reads and stats files.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

MAX_BUNDLE_SIZE_BYTES = 20 * 1024 * 1024  # 20MB -- a SKILL.md bundle is text + small assets, not a model checkpoint
MAX_FILE_COUNT = 500

_FRONTMATTER_NAME_PATTERN = re.compile(r"^name\s*:\s*.+$", re.MULTILINE)


@dataclass
class ValidationResult:
    passed: bool
    findings: list[str]


def _iter_files(bundle_root: Path):
    for path in bundle_root.rglob("*"):
        if path.is_file() or path.is_symlink():
            yield path


def validate_quarantined_bundle(bundle_root: Path) -> ValidationResult:
    findings: list[str] = []
    bundle_root = bundle_root.resolve()

    skill_md = bundle_root / "SKILL.md"
    if not skill_md.is_file():
        findings.append("SKILL.md not found at bundle root")
        return ValidationResult(passed=False, findings=findings)

    text = skill_md.read_text(encoding="utf-8", errors="replace")
    if not text.lstrip().startswith("---"):
        findings.append("SKILL.md has no YAML frontmatter block")
    elif not _FRONTMATTER_NAME_PATTERN.search(text.split("---", 2)[1] if text.count("---") >= 2 else ""):
        findings.append("SKILL.md frontmatter has no 'name' field")

    total_size = 0
    file_count = 0
    for path in _iter_files(bundle_root):
        file_count += 1
        if file_count > MAX_FILE_COUNT:
            findings.append(f"bundle contains more than {MAX_FILE_COUNT} files")
            break

        if path.is_symlink():
            target = path.resolve()
            try:
                target.relative_to(bundle_root)
            except ValueError:
                findings.append(f"symlink escapes bundle root: {path.relative_to(bundle_root)}")
            continue

        # Path-traversal check: every real file must resolve to somewhere
        # under bundle_root -- catches '..' components and absolute paths
        # smuggled in via a crafted archive/manifest.
        try:
            path.resolve().relative_to(bundle_root)
        except ValueError:
            findings.append(f"file escapes bundle root: {path}")
            continue

        try:
            total_size += path.stat().st_size
        except OSError as exc:
            findings.append(f"could not stat {path.relative_to(bundle_root)}: {exc}")

    if total_size > MAX_BUNDLE_SIZE_BYTES:
        findings.append(f"bundle size {total_size} bytes exceeds limit {MAX_BUNDLE_SIZE_BYTES}")

    return ValidationResult(passed=not findings, findings=findings)


def compute_bundle_hash(bundle_root: Path) -> str:
    """Deterministic content hash over every file's relative path + bytes,
    so an update is detected even if only one file inside the bundle
    changed."""
    hasher = hashlib.sha256()
    for path in sorted(_iter_files(bundle_root), key=lambda p: str(p.relative_to(bundle_root))):
        if path.is_symlink():
            continue
        rel = str(path.relative_to(bundle_root)).replace("\\", "/")
        hasher.update(rel.encode("utf-8"))
        hasher.update(path.read_bytes())
    return hasher.hexdigest()
