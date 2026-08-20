"""Downloads a catalog skill's bundle into quarantine, validates it, and
promotes it into the Obsidian vault's Skills directory (Phase 8). No
`hermes` CLI is installed on this machine (checked at build time), so this
is the documented HTTP fallback: GitHub's Contents API, recursively, for
catalog records that carry a resolvable `repo`+`path`.

Known limitation (see FINAL REPORT): clawhub / browse-sh / lobehub-sourced
records carry no `repo`/`path` in the Hermes catalog at all -- there is
nothing here to download from for those ~70k records without scraping
each platform directly, which the task explicitly said not to do ("don't
write ten unrelated scrapers"). Only official/skills.sh/github/
claude-marketplace records (~20.5k) are installable via this path.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import httpx

from skill_registry.validation import ValidationResult, compute_bundle_hash, validate_quarantined_bundle

GITHUB_API = "https://api.github.com"


class UnresolvableSourceError(RuntimeError):
    """Raised when a catalog record has no repo/path to download from --
    never silently invents a download location."""


@dataclass
class DownloadResult:
    quarantine_path: Path
    file_count: int


def _is_github_resolvable(record: dict) -> bool:
    repo = record.get("repo") or ""
    path = record.get("path")
    return bool(repo) and "/" in repo and path is not None


async def download_to_quarantine(record: dict, quarantine_root: Path, timeout_seconds: float = 30.0) -> DownloadResult:
    if not _is_github_resolvable(record):
        raise UnresolvableSourceError(
            f"catalog record '{record.get('identifier')}' (source={record.get('source')}) has no "
            "resolvable repo/path -- cannot download without a source-specific scraper"
        )

    repo = record["repo"]
    path = record.get("path") or ""
    dest = quarantine_root / f"{uuid4().hex}"
    dest.mkdir(parents=True, exist_ok=True)

    file_count = 0
    async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
        file_count = await _download_dir(client, repo, path, dest)

    if file_count == 0:
        shutil.rmtree(dest, ignore_errors=True)
        raise UnresolvableSourceError(f"no files found at {repo}/{path} via GitHub Contents API")

    return DownloadResult(quarantine_path=dest, file_count=file_count)


async def _download_dir(client: httpx.AsyncClient, repo: str, path: str, dest: Path) -> int:
    url = f"{GITHUB_API}/repos/{repo}/contents/{path}"
    resp = await client.get(url, headers={"Accept": "application/vnd.github+json"})
    if resp.status_code == 404:
        # `path` may itself be a single file (some catalog entries point
        # directly at a SKILL.md), not a directory.
        return await _download_single_file(client, repo, path, dest)
    if resp.status_code != 200:
        raise UnresolvableSourceError(f"GitHub Contents API returned {resp.status_code} for {repo}/{path}")

    entries = resp.json()
    if isinstance(entries, dict):
        entries = [entries]

    count = 0
    for entry in entries:
        entry_path = entry["path"]
        rel = entry_path[len(path) :].lstrip("/") if path else entry_path
        local_path = dest / rel if rel else dest / Path(entry_path).name

        if entry["type"] == "dir":
            count += await _download_dir(client, repo, entry_path, dest)
        elif entry["type"] == "file":
            download_url = entry.get("download_url")
            if not download_url:
                continue
            file_resp = await client.get(download_url)
            if file_resp.status_code != 200:
                continue
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(file_resp.content)
            count += 1
    return count


async def _download_single_file(client: httpx.AsyncClient, repo: str, path: str, dest: Path) -> int:
    for branch in ("main", "master"):
        raw_url = f"https://raw.githubusercontent.com/{repo}/{branch}/{path}"
        resp = await client.get(raw_url)
        if resp.status_code == 200:
            local_path = dest / "SKILL.md"
            local_path.write_bytes(resp.content)
            return 1
    return 0


def validate_quarantine(quarantine_path: Path) -> ValidationResult:
    return validate_quarantined_bundle(quarantine_path)


def bundle_hash(quarantine_path: Path) -> str:
    return compute_bundle_hash(quarantine_path)


def promote_to_vault(quarantine_path: Path, dest_dir: Path) -> None:
    """Moves a validated bundle from quarantine into its final category
    folder inside the vault. Never called before validate_quarantine()
    passes."""
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(quarantine_path), str(dest_dir))


def remove_quarantine(quarantine_path: Path) -> None:
    shutil.rmtree(quarantine_path, ignore_errors=True)
