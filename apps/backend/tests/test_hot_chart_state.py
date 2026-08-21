from __future__ import annotations

from datetime import UTC, datetime, timedelta

from assistant.chart_analysis import ChartAnalysisResult
from assistant.hot_chart_state import (
    ChartIdentity,
    Freshness,
    HotChartState,
    _parse_timeframe_minutes,
)


def _result(**overrides) -> ChartAnalysisResult:
    base = dict(
        instrument="XAUUSD",
        timeframe="15M",
        market_context="Ranging near resistance.",
        key_levels=["Resistance: 4550", "Support: 4500"],
        possible_setup=None,
        invalidation="Break below 4500",
        risk_notes="Macro/event risk has not been checked in this chart-only analysis.",
        provider="claude_code",
        raw_text="{}",
        structured=True,
    )
    base.update(overrides)
    return ChartAnalysisResult(**base)


def _state(*, timeframe: str | None, analyzed_at: datetime) -> HotChartState:
    identity = ChartIdentity(chart_window_id="hwnd-1", symbol="XAUUSD", timeframe=timeframe)
    return HotChartState(
        identity=identity,
        analysis=_result(timeframe=timeframe or ""),
        screenshot_hash="abc123",
        source="vision",
        observed_at=analyzed_at.isoformat(),
        analyzed_at=analyzed_at.isoformat(),
    )


def test_parse_timeframe_minutes_handles_common_labels():
    assert _parse_timeframe_minutes("1M") == 1
    assert _parse_timeframe_minutes("5m") == 5
    assert _parse_timeframe_minutes("15M") == 15
    assert _parse_timeframe_minutes("4H") == 240
    assert _parse_timeframe_minutes("1D") == 1440
    assert _parse_timeframe_minutes("1W") == 10080


def test_parse_timeframe_minutes_returns_none_for_unrecognized_text():
    assert _parse_timeframe_minutes(None) is None
    assert _parse_timeframe_minutes("") is None
    assert _parse_timeframe_minutes("weekly chart") is None
    assert _parse_timeframe_minutes("Renko") is None


def test_sub_5m_timeframe_uses_short_thresholds():
    now = datetime.now(UTC)
    state = _state(timeframe="5M", analyzed_at=now - timedelta(seconds=10))
    assert state.freshness(now=now) == Freshness.HOT
    state = _state(timeframe="5M", analyzed_at=now - timedelta(seconds=30))
    assert state.freshness(now=now) == Freshness.WARM
    state = _state(timeframe="5M", analyzed_at=now - timedelta(seconds=60))
    assert state.freshness(now=now) == Freshness.STALE


def test_15m_timeframe_uses_moderate_thresholds():
    now = datetime.now(UTC)
    state = _state(timeframe="15M", analyzed_at=now - timedelta(seconds=45))
    assert state.freshness(now=now) == Freshness.HOT
    state = _state(timeframe="15M", analyzed_at=now - timedelta(seconds=120))
    assert state.freshness(now=now) == Freshness.WARM
    state = _state(timeframe="15M", analyzed_at=now - timedelta(seconds=200))
    assert state.freshness(now=now) == Freshness.STALE


def test_1h_plus_timeframe_uses_longer_thresholds():
    now = datetime.now(UTC)
    state = _state(timeframe="4H", analyzed_at=now - timedelta(minutes=2))
    assert state.freshness(now=now) == Freshness.HOT
    state = _state(timeframe="1D", analyzed_at=now - timedelta(minutes=8))
    assert state.freshness(now=now) == Freshness.WARM
    state = _state(timeframe="1D", analyzed_at=now - timedelta(minutes=15))
    assert state.freshness(now=now) == Freshness.STALE


def test_unrecognized_timeframe_uses_most_conservative_thresholds():
    now = datetime.now(UTC)
    state = _state(timeframe=None, analyzed_at=now - timedelta(seconds=60))
    assert state.freshness(now=now) == Freshness.STALE


def test_usable_for_rejects_symbol_mismatch():
    now = datetime.now(UTC)
    state = _state(timeframe="15M", analyzed_at=now - timedelta(seconds=5))
    other_symbol = ChartIdentity(chart_window_id="hwnd-1", symbol="EURUSD", timeframe="15M")
    assert state.usable_for(other_symbol, now=now) is False


def test_usable_for_rejects_timeframe_mismatch():
    now = datetime.now(UTC)
    state = _state(timeframe="15M", analyzed_at=now - timedelta(seconds=5))
    other_tf = ChartIdentity(chart_window_id="hwnd-1", symbol="XAUUSD", timeframe="5M")
    assert state.usable_for(other_tf, now=now) is False


def test_usable_for_rejects_window_mismatch():
    now = datetime.now(UTC)
    state = _state(timeframe="15M", analyzed_at=now - timedelta(seconds=5))
    other_window = ChartIdentity(chart_window_id="hwnd-2", symbol="XAUUSD", timeframe="15M")
    assert state.usable_for(other_window, now=now) is False


def test_usable_for_rejects_stale_state_even_with_matching_identity():
    now = datetime.now(UTC)
    state = _state(timeframe="5M", analyzed_at=now - timedelta(seconds=90))
    same_identity = ChartIdentity(chart_window_id="hwnd-1", symbol="XAUUSD", timeframe="5M")
    assert state.usable_for(same_identity, now=now) is False


def test_usable_for_accepts_hot_matching_identity():
    now = datetime.now(UTC)
    state = _state(timeframe="15M", analyzed_at=now - timedelta(seconds=5))
    same_identity = ChartIdentity(chart_window_id="hwnd-1", symbol="XAUUSD", timeframe="15M")
    assert state.usable_for(same_identity, now=now) is True
