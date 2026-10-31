from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def verifier():
    path = Path(__file__).resolve().parents[2] / "tools" / "verify_market_integration.py"
    spec = importlib.util.spec_from_file_location("verify_market_integration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_identity_requires_stable_process_and_compatibility_fields(verifier) -> None:
    ok, _detail = verifier.identity_is_well_formed(
        {
            "service": "tars-backend",
            "contract_version": 1,
            "instance_id": "0123456789abcdef",
            "build_id": "110d07e77060",
            "build_source": "git",
            "app_version": "1.0.0",
            "started_at": "2026-10-31T00:00:00+00:00",
        }
    )

    assert ok is True


def test_identity_rejects_missing_or_malformed_process_id(verifier) -> None:
    ok, detail = verifier.identity_is_well_formed(
        {
            "service": "tars-backend",
            "contract_version": 1,
            "instance_id": "not-a-process-id",
            "build_id": "1.0.0",
            "build_source": "version",
            "app_version": "1.0.0",
            "started_at": "2026-10-31T00:00:00+00:00",
        }
    )

    assert ok is False
    assert "not-a-process-id" in detail


def test_recorded_provenance_is_accepted(verifier) -> None:
    errors = verifier.provenance_errors(
        "chart",
        {
            "source": "RECORDED_STORE",
            "provider": "parquet_store",
            "observed_at": "2026-09-14T20:55:00+00:00",
            "replay": True,
            "is_live": False,
        },
    )

    assert errors == []


def test_replayed_payload_cannot_claim_live_provenance(verifier) -> None:
    errors = verifier.provenance_errors(
        "chart",
        {
            "source": "LIVE_FEED",
            "provider": "mt5",
            "observed_at": "2026-09-14T20:55:00+00:00",
            "replay": True,
            "is_live": True,
        },
    )

    assert any("source" in error for error in errors)
    assert any("provider" in error for error in errors)
    assert any("is_live" in error for error in errors)
