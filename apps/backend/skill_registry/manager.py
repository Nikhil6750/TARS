"""SkillManager -- the single entry point for the skill catalog + installed
skills (Phase 6). Wraps skill_registry.{catalog,db,installer,validation,
obsidian_notes,vault_writer} so the rest of TARS never touches those
modules directly. All search happens against the LOCAL catalog after
sync() -- no network request is made per user search (Phase 6).

One source of truth (Phase 5): the only copy of an installed SKILL.md
bundle lives under `<obsidian_vault>/TARS/Skills/<Category>/<slug>/`. This
manager does not maintain a second copy anywhere else; if Hermes's own CLI
is later installed, point its `skills.external_dirs` at the same
directory rather than letting it maintain its own copy.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from skill_registry import db as registry_db
from skill_registry import obsidian_notes, vault_writer
from skill_registry.catalog import (
    CatalogFetchError,
    CatalogPayload,
    fetch_hermes_catalog,
    load_catalog_from_gzip,
    save_gzip_snapshot,
)
from skill_registry.categorize import categorize, safe_folder_slug
from skill_registry.installer import (
    UnresolvableSourceError,
    bundle_hash,
    download_to_quarantine,
    promote_to_vault,
    remove_quarantine,
    validate_quarantine,
)


@dataclass
class SyncResult:
    sync_id: str
    record_count: int
    inserted: int
    updated: int
    unchanged: int
    raw_size_bytes: int
    compressed_size_bytes: int
    sha256: str
    duration_seconds: float
    acquisition_method: str


@dataclass
class InstallResult:
    identifier: str
    installed: bool
    local_path: str | None
    content_hash: str | None
    findings: list[str]


class SkillManager:
    def __init__(self, conn: aiosqlite.Connection, vault_path: str, catalog_snapshot_path: Path):
        self._conn = conn
        self._vault_path = vault_path
        self._catalog_snapshot_path = catalog_snapshot_path

    # ---- Phase 2/3: catalog sync -------------------------------------
    async def sync_catalog(self, *, allow_offline_fallback: bool = True) -> SyncResult:
        started = time.monotonic()
        sync_id = None
        try:
            try:
                payload = await fetch_hermes_catalog()
            except CatalogFetchError:
                if not allow_offline_fallback:
                    raise
                payload = load_catalog_from_gzip(self._catalog_snapshot_path)

            sync_id = await registry_db.start_sync_log(
                self._conn, payload.source_url, payload.acquisition_method
            )
            stats = await registry_db.upsert_catalog_records(self._conn, payload.skills, payload.version)
            compressed_size = save_gzip_snapshot(payload, self._catalog_snapshot_path)
            duration = time.monotonic() - started

            await registry_db.finish_sync_log(
                self._conn,
                sync_id,
                status="SUCCEEDED",
                record_count=payload.record_count,
                raw_size_bytes=len(payload.raw_bytes),
                compressed_size_bytes=compressed_size,
                sha256=payload.sha256,
                duration_seconds=duration,
            )
            return SyncResult(
                sync_id=sync_id,
                record_count=payload.record_count,
                inserted=stats.inserted,
                updated=stats.updated,
                unchanged=stats.unchanged,
                raw_size_bytes=len(payload.raw_bytes),
                compressed_size_bytes=compressed_size,
                sha256=payload.sha256,
                duration_seconds=duration,
                acquisition_method=payload.acquisition_method,
            )
        except Exception as exc:
            if sync_id is not None:
                await registry_db.finish_sync_log(
                    self._conn, sync_id, status="FAILED", duration_seconds=time.monotonic() - started, error=str(exc)
                )
            raise

    # ---- Phase 6/7: search & inspect ----------------------------------
    async def search_skills(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        installed = {row["identifier"] for row in await registry_db.list_installed(self._conn)}
        return await registry_db.search_catalog(self._conn, query, limit=limit, installed_identifiers=installed)

    async def get_skill(self, identifier: str) -> dict[str, Any] | None:
        return await registry_db.get_skill(self._conn, identifier)

    async def inspect_skill(self, identifier: str) -> dict[str, Any] | None:
        record = await registry_db.get_skill(self._conn, identifier)
        if record is None:
            return None
        installed = await registry_db.get_installed(self._conn, identifier)
        record = dict(record)
        record["installed"] = installed
        return record

    async def list_installed(self) -> list[dict[str, Any]]:
        return await registry_db.list_installed(self._conn)

    # ---- Phase 8: install / update / uninstall ------------------------
    async def install_skill(self, identifier: str) -> InstallResult:
        record = await registry_db.get_skill(self._conn, identifier)
        if record is None:
            return InstallResult(identifier=identifier, installed=False, local_path=None, content_hash=None, findings=["identifier not found in catalog"])

        vault_writer.ensure_vault_structure(self._vault_path)
        quarantine_root = vault_writer.quarantine_root(self._vault_path)
        quarantine_root.mkdir(parents=True, exist_ok=True)

        try:
            download = await download_to_quarantine(record, quarantine_root)
        except UnresolvableSourceError as exc:
            await registry_db.record_audit(self._conn, identifier, passed=False, findings=[str(exc)], quarantine_path=None)
            return InstallResult(identifier=identifier, installed=False, local_path=None, content_hash=None, findings=[str(exc)])

        validation = validate_quarantine(download.quarantine_path)
        await registry_db.record_audit(
            self._conn, identifier, passed=validation.passed, findings=validation.findings, quarantine_path=str(download.quarantine_path)
        )
        if not validation.passed:
            remove_quarantine(download.quarantine_path)
            return InstallResult(identifier=identifier, installed=False, local_path=None, content_hash=None, findings=validation.findings)

        content_hash = bundle_hash(download.quarantine_path)
        category = categorize(record)
        slug = safe_folder_slug(record.get("name") or identifier)
        dest_dir = vault_writer.category_dir(self._vault_path, category) / slug
        promote_to_vault(download.quarantine_path, dest_dir)

        rel_path = str(dest_dir.relative_to(Path(self._vault_path)))
        await registry_db.upsert_installed(
            self._conn,
            identifier=identifier,
            name=record["name"],
            category=category,
            local_path=rel_path,
            source=record.get("source"),
            trust_level=record.get("trust_level"),
            content_hash=content_hash,
            action="install",
        )
        await self.sync_obsidian_registry()
        return InstallResult(identifier=identifier, installed=True, local_path=rel_path, content_hash=content_hash, findings=[])

    async def update_skill(self, identifier: str) -> InstallResult:
        existing = await registry_db.get_installed(self._conn, identifier)
        if existing is None:
            return InstallResult(identifier=identifier, installed=False, local_path=None, content_hash=None, findings=["not currently installed"])
        result = await self.install_skill(identifier)
        return result

    async def uninstall_skill(self, identifier: str) -> bool:
        existing = await registry_db.get_installed(self._conn, identifier)
        if existing is None:
            return False
        bundle_dir = Path(self._vault_path) / existing["local_path"]
        if bundle_dir.is_dir():
            import shutil

            shutil.rmtree(bundle_dir)
        await registry_db.mark_uninstalled(self._conn, identifier, existing["content_hash"])
        await self.sync_obsidian_registry()
        return True

    async def audit_skill(self, identifier: str) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "SELECT * FROM skill_audit WHERE identifier = ? ORDER BY checked_at DESC LIMIT 1", (identifier,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def reindex_installed(self) -> int:
        """Recomputes content hashes for every installed bundle against
        what's actually on disk -- catches drift from a manual edit inside
        the vault rather than trusting the DB blindly."""
        from skill_registry.installer import bundle_hash as compute_hash

        installed = await registry_db.list_installed(self._conn)
        changed = 0
        for skill in installed:
            bundle_dir = Path(self._vault_path) / skill["local_path"]
            if not bundle_dir.is_dir():
                await registry_db.mark_uninstalled(self._conn, skill["identifier"], skill["content_hash"])
                changed += 1
                continue
            new_hash = compute_hash(bundle_dir)
            if new_hash != skill["content_hash"]:
                await registry_db.upsert_installed(
                    self._conn,
                    identifier=skill["identifier"],
                    name=skill["name"],
                    category=skill["category"],
                    local_path=skill["local_path"],
                    source=skill["source"],
                    trust_level=skill["trust_level"],
                    content_hash=new_hash,
                    action="update",
                    notes="reindex detected on-disk change",
                )
                changed += 1
        return changed

    # ---- Phase 9: Obsidian registry notes -----------------------------
    async def sync_obsidian_registry(self) -> None:
        vault_writer.ensure_vault_structure(self._vault_path)
        summary = await registry_db.catalog_summary(self._conn)
        installed = await registry_db.list_installed(self._conn)
        last_sync = await registry_db.get_last_sync(self._conn)

        # installed_skills doesn't carry its own description column -- the
        # catalog is the source of truth for it, looked up here so the
        # generated note reads cleanly instead of showing a blank field.
        for skill in installed:
            catalog_record = await registry_db.get_skill(self._conn, skill["identifier"])
            skill["description"] = catalog_record["description"] if catalog_record else ""

        vault_writer.write_text(
            vault_writer.registry_note_path(self._vault_path, "Skill Catalog.md"),
            obsidian_notes.render_skill_catalog_note(summary),
        )
        vault_writer.write_text(
            vault_writer.registry_note_path(self._vault_path, "Installed Skills.md"),
            obsidian_notes.render_installed_skills_note(installed),
        )
        vault_writer.write_text(
            vault_writer.registry_note_path(self._vault_path, "Sources.md"),
            obsidian_notes.render_sources_note(summary["sources"]),
        )
        vault_writer.write_text(
            vault_writer.registry_note_path(self._vault_path, "Sync Status.md"),
            obsidian_notes.render_sync_status_note(last_sync),
        )

        import json

        vault_writer.write_text(
            vault_writer.manifest_path(self._vault_path, "installed.json"),
            json.dumps(installed, indent=2, default=str),
        )
        vault_writer.write_text(
            vault_writer.manifest_path(self._vault_path, "sources.json"),
            json.dumps(summary["sources"], indent=2, default=str),
        )
