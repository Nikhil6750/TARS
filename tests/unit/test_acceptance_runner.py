from __future__ import annotations

from pathlib import Path

import pytest

from tools.run_acceptance import PAID_KEY_NAMES, scan_logs, scrubbed_environment


def test_acceptance_environment_removes_paid_keys_and_uses_invalid_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in PAID_KEY_NAMES:
        monkeypatch.setenv(name, "must-be-removed")

    environment = scrubbed_environment(tmp_path, "sentinel")

    assert all(environment[name] == "" for name in PAID_KEY_NAMES)
    assert environment["TARS_ACCEPTANCE_ZERO_PAID_KEYS"] == "1"
    vault_path = Path(environment["OBSIDIAN_VAULT_PATH"])
    assert vault_path.is_file(), "vault path must deliberately be invalid as a directory"


def test_log_scanner_detects_secret_sentinel(tmp_path: Path) -> None:
    clean = tmp_path / "clean.log"
    leaked = tmp_path / "leaked.log"
    clean.write_text("ordinary diagnostics", encoding="utf-8")
    leaked.write_text("prefix secret-value suffix", encoding="utf-8")

    scan_logs([clean], "secret-value")
    with pytest.raises(RuntimeError, match="leaked"):
        scan_logs([clean, leaked], "secret-value")
