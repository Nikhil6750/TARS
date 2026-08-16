from __future__ import annotations

from pathlib import Path

from tools.security_checks import (
    find_fabricated_metric_claims,
    find_live_execution_operations,
)


def test_execution_surface_detector_flags_mutating_order_paths() -> None:
    document = {
        "paths": {
            "/api/orders": {"post": {}},
            "/api/events": {"post": {}},
            "/api/execution": {"delete": {}},
        }
    }
    assert find_live_execution_operations(document) == [
        "DELETE /api/execution",
        "POST /api/orders",
    ]


def test_execution_surface_detector_ignores_read_only_documentation() -> None:
    document = {
        "paths": {
            "/api/order-history": {"get": {}},
            "/api/events/{event_id}/invalidate": {"post": {}},
        }
    }
    assert find_live_execution_operations(document) == []


def test_metric_scanner_flags_real_mode_claims_but_ignores_mock_fixture(
    tmp_path: Path,
) -> None:
    components = tmp_path / "components"
    services = tmp_path / "services"
    components.mkdir()
    services.mkdir()
    (components / "Dashboard.tsx").write_text(
        "export const claim = 'Realized Sharpe 2.12';", encoding="utf-8"
    )
    (services / "mock-generator.ts").write_text(
        "export const demo = 'DSR 1.8';", encoding="utf-8"
    )

    findings = find_fabricated_metric_claims(tmp_path)

    assert len(findings) == 1
    assert findings[0].startswith("components/Dashboard.tsx:")
