from __future__ import annotations

from assistant.router import (
    _ACTIVE_SETUPS_PATTERN,
    _ATTENTION_PATTERN,
    _format_active_setups,
    _format_attention_summary,
)


def test_active_setups_pattern_matches_expected_phrasings():
    assert _ACTIVE_SETUPS_PATTERN.search("show active setups")
    assert _ACTIVE_SETUPS_PATTERN.search("What's active right now?")
    assert not _ACTIVE_SETUPS_PATTERN.search("what requires my attention")


def test_attention_pattern_matches_expected_phrasings():
    assert _ATTENTION_PATTERN.search("what requires my attention?")
    assert _ATTENTION_PATTERN.search("anything need attention")
    assert not _ATTENTION_PATTERN.search("show active setups")


def test_format_active_setups_empty():
    assert "no active setups" in _format_active_setups([])


def test_format_active_setups_includes_deterministic_fields_only():
    events = [
        {
            "symbol": "ES",
            "state": "SETUP_VALID",
            "direction": "LONG",
            "entry": 5300.0,
            "stop_loss": 5290.0,
            "take_profit": 5320.0,
            "risk_reward": 2.0,
        }
    ]
    text = _format_active_setups(events)
    assert "ES" in text
    assert "5300.0" in text
    assert "5290.0" in text
    assert "confidence" not in text.lower()


def test_format_attention_summary_empty():
    assert "Nothing" in _format_attention_summary([], [])


def test_format_attention_summary_includes_warnings():
    active = [{"symbol": "ES", "validation_status": "VALID"}]
    warnings = [{"symbol": "XAUUSD", "state": "RISK_WARNING", "warnings": ["elevated volatility"]}]
    text = _format_attention_summary(active, warnings)
    assert "ES" in text
    assert "XAUUSD" in text
    assert "elevated volatility" in text
