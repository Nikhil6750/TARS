"""Deterministic Tauri manifest and package compatibility checks."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import tomllib

ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "apps" / "web"
TAURI_ROOT = WEB_ROOT / "src-tauri"
SEMVER = re.compile(r"(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)")
NPM_RUST_PAIRS = (
    ("@tauri-apps/api", "tauri"),
    ("@tauri-apps/plugin-notification", "tauri-plugin-notification"),
    ("@tauri-apps/plugin-global-shortcut", "tauri-plugin-global-shortcut"),
)


def parse_version(value: str, label: str) -> tuple[int, int, int]:
    match = SEMVER.search(value)
    if match is None:
        raise ValueError(f"{label} has no concrete semantic version: {value!r}")
    return tuple(int(match[name]) for name in ("major", "minor", "patch"))


def dependency_version(table: dict[str, Any], name: str) -> str:
    value = table.get(name)
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("version"), str):
        return value["version"]
    raise ValueError(f"Cargo dependency {name!r} has no version")


def collect_tauri_issues(
    web_root: Path = WEB_ROOT, tauri_root: Path = TAURI_ROOT
) -> list[str]:
    issues: list[str] = []
    package_path = web_root / "package.json"
    lock_path = web_root / "package-lock.json"
    cargo_path = tauri_root / "Cargo.toml"
    config_path = tauri_root / "tauri.conf.json"

    for required in (package_path, lock_path, cargo_path, config_path):
        if not required.is_file():
            issues.append(f"missing required manifest: {required}")
    if issues:
        return issues

    package = json.loads(package_path.read_text(encoding="utf-8"))
    package_lock = json.loads(lock_path.read_text(encoding="utf-8"))
    cargo = tomllib.loads(cargo_path.read_text(encoding="utf-8"))
    config = json.loads(config_path.read_text(encoding="utf-8"))

    declared_npm = package.get("dependencies", {}) | package.get("devDependencies", {})
    locked_npm: dict[str, str] = {}
    for npm_name, _ in NPM_RUST_PAIRS:
        if npm_name not in declared_npm:
            issues.append(f"missing npm Tauri dependency: {npm_name}")
            continue
        lock_entry = package_lock.get("packages", {}).get(f"node_modules/{npm_name}")
        if not isinstance(lock_entry, dict) or not isinstance(lock_entry.get("version"), str):
            issues.append(f"missing lockfile resolution for {npm_name}")
            continue
        locked_npm[npm_name] = lock_entry["version"]

    cli_name = "@tauri-apps/cli"
    cli_entry = package_lock.get("packages", {}).get(f"node_modules/{cli_name}")
    if not isinstance(cli_entry, dict) or not isinstance(cli_entry.get("version"), str):
        issues.append(f"missing lockfile resolution for {cli_name}")
    elif "@tauri-apps/api" in locked_npm:
        api_minor = parse_version(locked_npm["@tauri-apps/api"], "Tauri API")[:2]
        cli_minor = parse_version(cli_entry["version"], "Tauri CLI")[:2]
        if api_minor != cli_minor:
            issues.append(
                f"npm Tauri API/CLI minor mismatch: {locked_npm['@tauri-apps/api']} "
                f"vs {cli_entry['version']}"
            )

    cargo_dependencies = cargo.get("dependencies", {})
    for npm_name, rust_name in NPM_RUST_PAIRS:
        try:
            rust_version = dependency_version(cargo_dependencies, rust_name)
        except ValueError as exc:
            issues.append(str(exc))
            continue
        npm_version = locked_npm.get(npm_name)
        if npm_version is None:
            continue
        if parse_version(rust_version, rust_name)[:2] != parse_version(
            npm_version, npm_name
        )[:2]:
            issues.append(
                f"Tauri Rust/npm minor mismatch: {rust_name} {rust_version} "
                f"vs {npm_name} {npm_version}"
            )

    if not (tauri_root / "Cargo.lock").is_file():
        issues.append("missing src-tauri/Cargo.lock for reproducible application builds")
    build_rs = tauri_root / "build.rs"
    if not build_rs.is_file():
        issues.append("missing src-tauri/build.rs required to run tauri-build")
    for rust_source in (tauri_root / "src" / "main.rs", tauri_root / "src" / "lib.rs"):
        if not rust_source.is_file():
            issues.append(f"missing Rust entry point: {rust_source}")

    identifier = config.get("identifier")
    if not isinstance(identifier, str) or identifier.count(".") < 2:
        issues.append("tauri.conf.json identifier must be reverse-domain style")
    build = config.get("build")
    if not isinstance(build, dict):
        issues.append("tauri.conf.json build must be an object")
    else:
        scripts = package.get("scripts", {})
        if build.get("beforeBuildCommand") != "npm run build" or "build" not in scripts:
            issues.append("Tauri beforeBuildCommand must invoke the production web build")
        if build.get("frontendDist") != "../dist":
            issues.append("Tauri frontendDist must reference the shared Vite dist")

    tray_icon = config.get("app", {}).get("trayIcon", {}).get("iconPath")
    if not isinstance(tray_icon, str) or not (tauri_root / tray_icon).is_file():
        issues.append(f"configured tray icon does not exist: {tray_icon!r}")
    return issues


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cargo-check",
        action="store_true",
        help="run cargo check when the Rust toolchain is installed",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    issues = collect_tauri_issues()
    for issue in issues:
        print(f"TAURI_COMPAT_ERROR: {issue}", file=sys.stderr)

    cargo_result = 0
    if args.cargo_check:
        cargo = shutil.which("cargo")
        if cargo is None:
            print("TAURI_NATIVE_CHECK_SKIPPED: cargo is not installed")
        elif not issues:
            cargo_result = subprocess.run(
                [cargo, "check", "--manifest-path", str(TAURI_ROOT / "Cargo.toml")],
                cwd=ROOT,
                check=False,
            ).returncode

    raise SystemExit(1 if issues or cargo_result else 0)


if __name__ == "__main__":
    main()
