"""Generated Pydantic models must preserve canonical schema strictness."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from tools.generated.python.trading_event import TARSTradingEvent

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"


def load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", sorted((FIXTURES / "valid").glob("*.json")))
def test_generated_model_accepts_valid_contract_fixture(path: Path) -> None:
    model = TARSTradingEvent.model_validate(load_json(path))
    assert model.schema_version == "1.0.0"


@pytest.mark.parametrize("path", sorted((FIXTURES / "malformed").glob("*.json")))
def test_generated_model_rejects_malformed_contract_fixture(path: Path) -> None:
    with pytest.raises(ValidationError):
        TARSTradingEvent.model_validate(load_json(path))
