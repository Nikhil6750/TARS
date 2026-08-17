"""Canonical positive and negative trading-event examples."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
SCHEMA_PATH = ROOT / "contracts" / "trading-event.schema.json"
VALIDATOR = Draft202012Validator(
    json.loads(SCHEMA_PATH.read_text(encoding="utf-8")),
    format_checker=FormatChecker(),
)

VALID_STATES = {
    "idle.json": "IDLE",
    "setup_developing.json": "SETUP_DEVELOPING",
    "setup_valid.json": "SETUP_VALID",
    "setup_invalidated.json": "SETUP_INVALIDATED",
    "risk_warning.json": "RISK_WARNING",
    "system_warning.json": "SYSTEM_WARNING",
}

EXPECTED_FAILURES = {
    "missing_required.json": "required",
    "extra_property.json": "additionalProperties",
    "invalid_state.json": "enum",
    "bad_direction.json": "enum",
    "incorrect_numeric_type.json": "type",
    "malformed_timestamp.json": "format",
    "contract_version_mismatch.json": "const",
}


def load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("schema_path", sorted((ROOT / "contracts").glob("*.schema.json")))
def test_canonical_schema_is_valid_draft_2020_12(schema_path: Path) -> None:
    Draft202012Validator.check_schema(json.loads(schema_path.read_text(encoding="utf-8")))


@pytest.mark.parametrize(("filename", "state"), VALID_STATES.items())
def test_valid_lifecycle_fixture(filename: str, state: str) -> None:
    instance = load_json(FIXTURES / "valid" / filename)
    assert instance["state"] == state
    VALIDATOR.validate(instance)


@pytest.mark.parametrize(("filename", "validator_name"), EXPECTED_FAILURES.items())
def test_malformed_fixture_is_rejected(filename: str, validator_name: str) -> None:
    instance = load_json(FIXTURES / "malformed" / filename)
    errors = list(VALIDATOR.iter_errors(instance))
    assert errors, f"{filename} unexpectedly passed the canonical schema"
    assert validator_name in {error.validator for error in errors}


def test_fixture_inventory_has_no_unreviewed_files() -> None:
    assert {path.name for path in (FIXTURES / "valid").glob("*.json")} == set(
        VALID_STATES
    )
    assert {path.name for path in (FIXTURES / "malformed").glob("*.json")} == set(
        EXPECTED_FAILURES
    )
