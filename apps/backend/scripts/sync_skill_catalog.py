"""Manual network integration script (Phase 14/15) -- runs a REAL sync
against the live Hermes catalog and a REAL search/install/uninstall pass
against the configured Obsidian vault. Not part of the automated test
suite (which must not depend on the live Hermes server); run this by hand:

    cd apps/backend
    python scripts/sync_skill_catalog.py [--install <identifier>]

Prints every number it reports -- nothing here is fabricated; if a step
fails, it says so and stops rather than guessing.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aiosqlite

from app.config import get_settings
from skill_registry.manager import SkillManager
from storage.migrator import run_migrations


async def main(install_identifier: str | None, uninstall_identifier: str | None) -> None:
    settings = get_settings()
    run_migrations(settings.sqlite_path)
    conn = await aiosqlite.connect(str(settings.sqlite_path))
    conn.row_factory = aiosqlite.Row

    catalog_snapshot = Path(__file__).resolve().parents[1] / "data" / "catalog" / "hermes-skills-index.json.gz"
    manager = SkillManager(conn, settings.obsidian_vault_path, catalog_snapshot)

    print(f"Vault path: {settings.obsidian_vault_path}")
    print(f"SQLite path: {settings.sqlite_path}")
    print("--- Running real catalog sync ---")
    result = await manager.sync_catalog()
    print(f"acquisition_method: {result.acquisition_method}")
    print(f"record_count: {result.record_count}")
    print(f"inserted: {result.inserted}  updated: {result.updated}  unchanged: {result.unchanged}")
    print(f"raw_size_bytes: {result.raw_size_bytes}")
    print(f"compressed_size_bytes: {result.compressed_size_bytes}")
    print(f"sha256: {result.sha256}")
    print(f"duration_seconds: {result.duration_seconds:.2f}")

    print("\n--- Sample searches ---")
    for query in ["python", "github", "windows automation", "kubernetes", "data engineering", "research", "trading"]:
        results = await manager.search_skills(query, limit=5)
        print(f"\nQuery: {query!r} -> {len(results)} result(s)")
        for r in results:
            print(f"  {r['identifier']}  source={r['source']}  trust={r['trust_level']}")

    if install_identifier:
        print(f"\n--- Installing {install_identifier} ---")
        install_result = await manager.install_skill(install_identifier)
        print(f"installed: {install_result.installed}")
        print(f"local_path: {install_result.local_path}")
        print(f"content_hash: {install_result.content_hash}")
        if install_result.findings:
            print(f"findings: {install_result.findings}")

    if uninstall_identifier:
        print(f"\n--- Uninstalling {uninstall_identifier} ---")
        ok = await manager.uninstall_skill(uninstall_identifier)
        print(f"uninstalled: {ok}")

    await manager.sync_obsidian_registry()
    print("\n--- Obsidian registry notes synced ---")

    await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--install", dest="install_identifier", default=None)
    parser.add_argument("--uninstall", dest="uninstall_identifier", default=None)
    args = parser.parse_args()
    asyncio.run(main(args.install_identifier, args.uninstall_identifier))
