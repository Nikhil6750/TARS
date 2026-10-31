from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="native verifier is Windows-only")


@pytest.fixture(scope="module")
def verifier():
    path = Path(__file__).resolve().parents[2] / "tools" / "verify_market_desktop.py"
    spec = importlib.util.spec_from_file_location("verify_market_desktop", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def window(verifier, *, left: int, top: int, width: int, height: int):
    return verifier.NativeWindow(
        hwnd=0,
        pid=123,
        title="TARS — Market Intelligence",
        left=left,
        top=top,
        right=left + width,
        bottom=top + height,
        visible=True,
    )


def test_minimized_offscreen_signature_is_classified_once(verifier) -> None:
    iconic = window(verifier, left=-32_000, top=-32_000, width=199, height=34)

    assert verifier.has_minimized_signature(iconic) is True


def test_small_onscreen_window_is_not_misclassified_as_minimized(verifier) -> None:
    popup = window(verifier, left=20, top=30, width=199, height=34)

    assert verifier.has_minimized_signature(popup) is False


def test_fullscreen_requires_the_complete_monitor_rectangle(verifier) -> None:
    monitor = verifier.MonitorRect(left=1920, top=0, right=3840, bottom=1080)
    exact = window(verifier, left=1920, top=0, width=1920, height=1080)
    wrong_origin = window(verifier, left=0, top=0, width=1920, height=1080)
    invisible_border = window(verifier, left=1911, top=-9, width=1938, height=1098)

    assert verifier.window_matches_monitor(exact, monitor) is True
    assert verifier.window_matches_monitor(wrong_origin, monitor) is False
    assert verifier.window_matches_monitor(invisible_border, monitor) is False


def test_mismatch_evidence_never_claims_an_exact_match(verifier) -> None:
    monitor = verifier.MonitorRect(left=0, top=0, right=1920, bottom=1080)
    bordered = window(verifier, left=-11, top=-11, width=1942, height=1102)

    evidence = verifier.rect_evidence(bordered, monitor)

    assert "window rect=(-11,-11)-(1931,1091) 1942x1102" in evidence
    assert "monitor rect=(0,0)-(1920,1080) 1920x1080" in evidence
    assert "exactly matches" not in evidence


def test_report_distinguishes_failure_from_automation_limitation(verifier) -> None:
    report = verifier.Report()
    report.check("app behavior", False, "application returned the wrong state")
    report.could_not_verify("automation behavior", "foreground was denied")

    assert report.failed == ["app behavior"]
    assert report.unverified == ["automation behavior"]
    assert report.summary() == "0/2 desktop checks passed; 1 failed; 1 not verified"
