"""TradingViewAdapter -- the mission's "one TradingViewAdapter contract"
delegating to existing, separately-tested skills (TradingViewControlSkill
for control, TradingSkill for capture/analyze) and HotChartStateStore for
read state. These tests fake the skills/store at the same seam the adapter
itself defines (`skill.execute(request)`), so they verify the adapter's own
delegation/argument-shaping logic -- not reimplementing coverage that
already exists in test_tradingview_control.py / test_skill_trading.py /
test_hot_chart_state_store.py.
"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.action_contracts import ActionResult, ActionStatus, RiskLevel
from assistant.hot_chart_state import ChartIdentity, HotChartState
from trading.tradingview_adapter import TradingViewAdapter


class _FakeSkill:
    """Records the ActionRequest it was called with and returns a scripted
    ActionResult -- the same shape the adapter's `_call` expects back from
    any real skill's `execute()`."""

    def __init__(self, name: str, result: ActionResult | None = None, *, verified=None):
        self.name = name
        self.calls: list = []
        self._result = result
        self._verified = verified or {"symbol": None, "timeframe": None, "verified_at": None}

    async def execute(self, request):
        self.calls.append(request)
        return self._result or ActionResult(
            request_id=request.id, status=ActionStatus.SUCCEEDED, risk_level=RiskLevel.READ_ONLY,
            summary="ok", data={}, started_at=datetime.now(UTC),
        )

    def verified_identity(self):
        return self._verified


class _FakeStore:
    def __init__(self, state: HotChartState | None = None):
        self._state = state

    async def get_latest_for_window(self, window_id: str):
        return self._state

    async def get_latest(self):
        return self._state


def _state(symbol="EURUSD", timeframe="15m", age_s=5) -> HotChartState:
    from datetime import timedelta

    now = datetime.now(UTC)
    analyzed = (now - timedelta(seconds=age_s)).isoformat()
    return HotChartState(
        identity=ChartIdentity(chart_window_id="999", symbol=symbol, timeframe=timeframe),
        analysis=SimpleNamespace(), screenshot_hash="abc", source="vision",
        observed_at=analyzed, analyzed_at=analyzed,
    )


def _adapter(tv_result=None, trading_result=None, store=None, tv_verified=None) -> tuple[TradingViewAdapter, _FakeSkill, _FakeSkill]:
    tv = _FakeSkill("tradingview", tv_result, verified=tv_verified)
    trading = _FakeSkill("trading", trading_result)
    adapter = TradingViewAdapter(tv, trading, store or _FakeStore())
    return adapter, tv, trading


async def test_status_delegates_to_tradingview_skill():
    adapter, tv, _ = _adapter()
    out = await adapter.status()
    assert out["status"] == "SUCCEEDED"
    assert tv.calls[0].skill == "tradingview"
    assert tv.calls[0].action == "status"


async def test_set_symbol_passes_symbol_argument():
    adapter, tv, _ = _adapter()
    await adapter.set_symbol("XAUUSD")
    assert tv.calls[0].action == "set_symbol"
    assert tv.calls[0].arguments == {"symbol": "XAUUSD"}


async def test_set_timeframe_passes_timeframe_argument():
    adapter, tv, _ = _adapter()
    await adapter.set_timeframe("1h")
    assert tv.calls[0].action == "set_timeframe"
    assert tv.calls[0].arguments == {"timeframe": "1h"}


async def test_focus_delegates_to_tradingview_skill():
    adapter, tv, _ = _adapter()
    await adapter.focus()
    assert tv.calls[0].action == "focus"


async def test_capture_chart_delegates_to_trading_skill():
    adapter, _, trading = _adapter()
    await adapter.capture_chart()
    assert trading.calls[0].skill == "trading"
    assert trading.calls[0].action == "capture_chart"


async def test_analyze_chart_passes_question():
    adapter, _, trading = _adapter()
    await adapter.analyze_chart("what changed")
    assert trading.calls[0].action == "analyze_active_chart"
    assert trading.calls[0].arguments == {"question": "what changed"}


async def test_current_symbol_and_timeframe_read_from_store(monkeypatch):
    import trading.tradingview_adapter as mod

    monkeypatch.setattr(mod, "resolve_window", lambda target: (123, "chrome.exe", "EURUSD - TradingView"))
    adapter, _, _ = _adapter(store=_FakeStore(_state(symbol="EURUSD", timeframe="15m")))
    assert await adapter.current_symbol() == "EURUSD"
    assert await adapter.current_timeframe() == "15m"


async def test_current_symbol_is_none_when_window_not_found(monkeypatch):
    import trading.tradingview_adapter as mod
    from app.action_contracts import SkillExecutionError

    def raise_not_found(target):
        raise SkillExecutionError("not found")

    monkeypatch.setattr(mod, "resolve_window", raise_not_found)
    adapter, _, _ = _adapter()
    assert await adapter.current_symbol() is None
    assert await adapter.current_timeframe() is None


async def test_monitor_chart_reports_not_found_when_nothing_observed():
    adapter, _, _ = _adapter(store=_FakeStore(None))
    out = await adapter.monitor_chart()
    assert out == {"monitoring": False, "state": "NOT FOUND", "detail": "No chart window observed yet",
                   "symbol": None, "timeframe": None,
                   "identity_source": "vision", "vision_identity_conflict": False}


async def test_monitor_chart_reports_monitoring_when_fresh():
    adapter, _, _ = _adapter(store=_FakeStore(_state(symbol="XAUUSD", timeframe="5m", age_s=3)))
    out = await adapter.monitor_chart()
    assert out["monitoring"] is True
    assert out["state"] == "MONITORING"
    assert out["symbol"] == "XAUUSD"
    assert out["freshness"] == "hot"


async def test_monitor_chart_reports_not_found_when_stale():
    adapter, _, _ = _adapter(store=_FakeStore(_state(symbol="XAUUSD", timeframe="5m", age_s=9999)))
    out = await adapter.monitor_chart()
    assert out["monitoring"] is False
    assert out["state"] == "NOT FOUND"
    assert out["freshness"] == "stale"


# ---- mission section 2: verified structured identity outranks vision ----

async def test_monitor_chart_prefers_verified_timeframe_over_disagreeing_vision():
    """The exact live scenario: TradingView was deterministically set to
    5m and verified, but the background vision watcher's chart read says
    6m (a misread tiny glyph). The verified value must win, and the
    disagreement must be surfaced, never silently swapped."""
    adapter, _, _ = _adapter(
        store=_FakeStore(_state(symbol="XAUUSD", timeframe="6m", age_s=3)),
        tv_verified={"symbol": "XAUUSD", "timeframe": "5m", "verified_at": "2026-01-01T00:00:00Z"},
    )
    out = await adapter.monitor_chart()
    assert out["symbol"] == "XAUUSD"
    assert out["timeframe"] == "5m"
    assert out["identity_source"] == "verified"
    assert out["vision_identity_conflict"] is True


async def test_monitor_chart_uses_vision_for_a_field_with_no_verified_value():
    """Only the timeframe was ever deterministically verified -- the
    symbol field has no verified value, so it falls through to vision."""
    adapter, _, _ = _adapter(
        store=_FakeStore(_state(symbol="EURUSD", timeframe="1h", age_s=3)),
        tv_verified={"symbol": None, "timeframe": "1h", "verified_at": "2026-01-01T00:00:00Z"},
    )
    out = await adapter.monitor_chart()
    assert out["symbol"] == "EURUSD"  # from vision, no verified symbol to prefer
    assert out["timeframe"] == "1h"  # verified matches vision -- no conflict
    assert out["vision_identity_conflict"] is False


async def test_monitor_chart_reports_verified_identity_even_with_no_vision_at_all():
    adapter, _, _ = _adapter(
        store=_FakeStore(None),
        tv_verified={"symbol": "GBPUSD", "timeframe": "15m", "verified_at": "2026-01-01T00:00:00Z"},
    )
    out = await adapter.monitor_chart()
    assert out["symbol"] == "GBPUSD"
    assert out["timeframe"] == "15m"
    assert out["identity_source"] == "verified"
    assert out["vision_identity_conflict"] is False
