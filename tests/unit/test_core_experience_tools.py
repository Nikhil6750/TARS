from __future__ import annotations

from pathlib import Path

from tools.assistant_quality_benchmark import (
    QualityCase,
    compose_speech,
    evaluate_response,
    load_corpus,
)
from tools.capture_native_tars import (
    NativeWindow,
    choose_main_window,
    navigation_captures_are_distinct,
)
from tools.core_experience_checks import inspect_source

ROOT = Path(__file__).resolve().parents[2]


def test_native_window_selection_rejects_plugin_helper_windows() -> None:
    windows = [
        NativeWindow(1, 10, "", 0, 0, 14, 14, True),
        NativeWindow(2, 10, "", 0, 0, 1100, 780, False),
        NativeWindow(3, 10, "TARS", 20, 20, 400, 200, True),
        NativeWindow(4, 10, "TARS", 20, 20, 1120, 800, True),
    ]
    assert choose_main_window(windows) == windows[3]


def test_navigation_evidence_requires_four_distinct_captures() -> None:
    distinct = [
        {"capture_sha256": "chat"},
        {"capture_sha256": "workspace"},
        {"capture_sha256": "memory"},
        {"capture_sha256": "settings"},
    ]
    assert navigation_captures_are_distinct(distinct) is True
    distinct[-1]["capture_sha256"] = "chat"
    assert navigation_captures_are_distinct(distinct) is False


def test_source_diagnostics_reproduce_core_experience_blockers() -> None:
    finding_ids = {finding.finding_id for finding in inspect_source(ROOT)}
    assert {
        "launcher.false_ready",
        "launcher.shared_stale_binary",
        "launcher.no_native_window_verification",
        "native.default_compact_clips_workstation",
        "voice.ui_hardcoded_provider_status",
        "wake.incomplete_state_machine",
        "wake.single_utterance_command_dropped",
        "speech.raw_stream_markdown_to_tts",
        "speech.display_markdown_to_tts",
        "answers.no_response_quality_contract",
    } <= finding_ids


def test_quality_corpus_has_thirty_unique_cases_and_required_categories() -> None:
    corpus = load_corpus()
    assert len(corpus) == 30
    assert len({case.id for case in corpus}) == 30
    assert {
        "short factual explanation",
        "reasoning",
        "coding",
        "debugging",
        "trading epistemics",
        "insufficient-data request",
        "structured professional response",
        "conversational response",
        "user asks for short",
        "complex multi-part request",
    } == {case.category for case in corpus}


def test_speech_composer_removes_markdown_urls_paths_and_code_blocks() -> None:
    display = """### XAUUSD · 15M

**Trade Status**
- **NO VALIDATED TRADE**
- Details: https://example.test/report
- File: C:\\TARS\\scratch\\chart.png

```python
print('do not read this')
```
"""
    speech = compose_speech(display)
    assert "NO VALIDATED TRADE" in speech
    assert "Code example omitted" in speech
    for marker in ("**", "###", "```", "http", "C:\\TARS", "- "):
        assert marker not in speech


def test_quality_contract_flags_internal_leakage_and_missing_uncertainty() -> None:
    case = QualityCase(
        id="missing",
        category="insufficient-data request",
        prompt="What happened today?",
        max_words=50,
        must_include_any=["don't have"],
        forbidden=[],
        requires_uncertainty=True,
    )
    result = evaluate_response(
        case,
        "The provider subprocess exit code was 1. Everything probably rallied.",
    )
    assert result.directness is True
    assert result.completeness is False
    assert result.uncertainty is False
    assert result.user_mode_cleanliness is False
