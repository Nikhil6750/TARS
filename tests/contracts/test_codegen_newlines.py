"""Cross-platform newline regressions for generated contract artifacts."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tools import generate_contracts as codegen


def _artifact_bytes(directory: Path) -> dict[Path, bytes]:
    return {
        relative_path: (directory / relative_path).read_bytes()
        for relative_path in codegen._relative_files(directory)
    }


def test_generation_is_repeatedly_byte_identical_and_lf_only(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"

    codegen.generate(first)
    codegen.generate(second)

    first_bytes = _artifact_bytes(first)
    assert first_bytes == _artifact_bytes(second)
    assert first_bytes
    assert all(b"\r" not in content for content in first_bytes.values())
    assert all(
        content == codegen._canonical_newlines(content)
        for content in first_bytes.values()
    )


def test_drift_check_accepts_only_crlf_equivalent_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    committed = tmp_path / "committed"
    generated = tmp_path / "generated"
    relative_path = Path("typescript/example.d.ts")
    (committed / relative_path).parent.mkdir(parents=True)
    (generated / relative_path).parent.mkdir(parents=True)
    (committed / relative_path).write_bytes(b"export type Example = string;\r\n")
    (generated / relative_path).write_bytes(b"export type Example = string;\n")
    monkeypatch.setattr(codegen, "COMMITTED_DIR", committed)

    codegen.check_drift(generated)

    (generated / relative_path).write_bytes(b"export type Example = number;\n")
    with pytest.raises(SystemExit, match="content differs: typescript/example.d.ts"):
        codegen.check_drift(generated)


def test_semantic_schema_change_still_fails_drift_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schemas = tmp_path / "contracts"
    committed = tmp_path / "committed"
    generated = tmp_path / "generated"
    shutil.copytree(codegen.SCHEMA_DIR, schemas)
    shutil.copytree(codegen.COMMITTED_DIR, committed)

    schema_path = schemas / "assistant-message.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema["properties"]["content"]["type"] = "integer"
    schema_path.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")

    monkeypatch.setattr(codegen, "SCHEMA_DIR", schemas)
    monkeypatch.setattr(codegen, "COMMITTED_DIR", committed)
    codegen.generate(generated)

    with pytest.raises(SystemExit, match="content differs"):
        codegen.check_drift(generated)
