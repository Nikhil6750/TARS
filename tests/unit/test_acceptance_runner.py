from __future__ import annotations

from pathlib import Path

import pytest

from tools.run_acceptance import (
    PAID_KEY_NAMES,
    assert_endpoint_unused,
    scan_logs,
    scrubbed_environment,
)


def test_acceptance_environment_removes_paid_keys_and_builds_provenance_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in PAID_KEY_NAMES:
        monkeypatch.setenv(name, "must-be-removed")

    environment = scrubbed_environment(tmp_path, "sentinel")

    assert all(environment[name] == "" for name in PAID_KEY_NAMES)
    assert environment["TARS_ACCEPTANCE_ZERO_PAID_KEYS"] == "1"
    vault_path = Path(environment["OBSIDIAN_VAULT_PATH"])
    source_id = environment["TARS_ACCEPTANCE_VAULT_SOURCE_ID"]
    note = vault_path / Path(source_id)
    assert vault_path.is_dir()
    assert "TARS_PROVENANCE_ANCHOR" in note.read_text(encoding="utf-8")


def test_log_scanner_detects_secret_sentinel(tmp_path: Path) -> None:
    clean = tmp_path / "clean.log"
    leaked = tmp_path / "leaked.log"
    clean.write_text("ordinary diagnostics", encoding="utf-8")
    leaked.write_text("prefix secret-value suffix", encoding="utf-8")

    scan_logs([clean], "secret-value")
    with pytest.raises(RuntimeError, match="leaked"):
        scan_logs([clean, leaked], "secret-value")


def test_process_owning_run_rejects_preexisting_service(monkeypatch) -> None:
    class ExistingResponse:
        status_code = 200

    monkeypatch.setattr("tools.run_acceptance.httpx.get", lambda *args, **kwargs: ExistingResponse())
    with pytest.raises(RuntimeError, match="existing service"):
        assert_endpoint_unused("http://127.0.0.1:8000/health")
