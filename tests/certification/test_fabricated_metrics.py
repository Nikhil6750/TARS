from __future__ import annotations

from pathlib import Path

from tools.security_checks import find_fabricated_metric_claims

ROOT = Path(__file__).resolve().parents[2]


def test_normal_frontend_source_has_no_fabricated_performance_claims() -> None:
    findings = find_fabricated_metric_claims(ROOT / "apps" / "web" / "src")
    assert not findings, "fabricated metrics exposed in real mode:\n" + "\n".join(findings)
