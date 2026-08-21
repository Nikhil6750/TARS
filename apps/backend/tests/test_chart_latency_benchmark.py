"""Unit tests for the pure logic in scripts/chart_latency_benchmark.py
(TARS Alexa-Speed Phase H). Only the pieces that don't need a live backend
or a real chart image -- the actual acceptance run is a manual/CI step
against a running backend, not something to fake here."""
from __future__ import annotations

from pathlib import Path

from scripts.chart_latency_benchmark import _guess_image_format, _percentile, _quality_check


def test_guess_image_format_recognizes_common_extensions():
    assert _guess_image_format(Path("chart.png")) == "image/png"
    assert _guess_image_format(Path("chart.bmp")) == "image/bmp"
    assert _guess_image_format(Path("chart.jpg")) == "image/jpeg"
    assert _guess_image_format(Path("chart.jpeg")) == "image/jpeg"


def test_guess_image_format_defaults_to_png_for_unknown_extension():
    assert _guess_image_format(Path("chart.weird")) == "image/png"


def test_percentile_matches_known_values():
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert _percentile(values, 50) == 30.0
    assert _percentile(values, 0) == 10.0
    assert _percentile(values, 100) == 50.0


def test_percentile_handles_empty_and_single_value():
    assert _percentile([], 50) is None
    assert _percentile([42.0], 50) == 42.0


def test_quality_check_passes_a_clean_result():
    result = {
        "structured": True,
        "instrument": "XAUUSD",
        "timeframe": "15M",
        "disclaimer": "Qualitative read, not a quant_brain-validated signal.",
        "speech_text": "Bias is Neutral.",
        "formatted_tars_text": "### STRUCTURE\n...",
    }
    assert _quality_check(result) == []


def test_quality_check_flags_a_missing_disclaimer():
    result = {"structured": False, "disclaimer": "", "speech_text": "", "formatted_tars_text": ""}
    failures = _quality_check(result)
    assert any("disclaimer" in f for f in failures)


def test_quality_check_flags_structured_result_with_no_instrument_or_timeframe():
    result = {
        "structured": True,
        "instrument": None,
        "timeframe": None,
        "disclaimer": "not a quant_brain-validated signal",
        "speech_text": "",
        "formatted_tars_text": "",
    }
    failures = _quality_check(result)
    assert any("instrument/timeframe" in f for f in failures)


def test_quality_check_flags_unsupported_certainty_language():
    result = {
        "structured": True,
        "instrument": "XAUUSD",
        "timeframe": "15M",
        "disclaimer": "not a quant_brain-validated signal",
        "speech_text": "Price will definitely reverse here.",
        "formatted_tars_text": "",
    }
    failures = _quality_check(result)
    assert any("certainty" in f for f in failures)
