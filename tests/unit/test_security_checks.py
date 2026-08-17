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


def test_metric_scanner_flags_fabricated_cue_and_honesty_badges(
    tmp_path: Path,
) -> None:
    components = tmp_path / "components"
    components.mkdir()
    (components / "CompanionHero.tsx").write_text(
        "<span>CUE: <span className=\"text-cyan-400 font-semibold\">100%</span></span>\n"
        "<span>HONESTY: <span className=\"text-cyan-400 font-semibold\">95%</span></span>",
        encoding="utf-8",
    )

    findings = find_fabricated_metric_claims(tmp_path)

    assert len(findings) == 2
    assert any("CUE" in finding for finding in findings)
    assert any("HONESTY" in finding for finding in findings)


def test_metric_scanner_ignores_adr_reference_that_is_not_a_labelled_metric(
    tmp_path: Path,
) -> None:
    components = tmp_path / "components"
    components.mkdir()
    (components / "trading-event.ts").write_text(
        "// Never add AI confidence or probability fields (ADR-004).",
        encoding="utf-8",
    )

    findings = find_fabricated_metric_claims(tmp_path)

    assert findings == []
