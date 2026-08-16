from __future__ import annotations

import json
from pathlib import Path

from tools.tauri_checks import collect_tauri_issues, parse_version

ROOT = Path(__file__).resolve().parents[2]


def test_semver_parser_uses_concrete_version() -> None:
    assert parse_version("^2.11.4", "cli") == (2, 11, 4)


def test_tauri_manifests_packages_and_build_metadata_are_compatible() -> None:
    issues = collect_tauri_issues(
        ROOT / "apps" / "web", ROOT / "apps" / "web" / "src-tauri"
    )
    assert not issues, "Tauri compatibility errors:\n" + "\n".join(issues)


def test_tauri_config_is_valid_json_with_v2_schema() -> None:
    path = ROOT / "apps" / "web" / "src-tauri" / "tauri.conf.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    assert config["$schema"].endswith("config.schema.json") or "/config/2" in config[
        "$schema"
    ]
    assert config["productName"]
    assert config["app"]["windows"]
