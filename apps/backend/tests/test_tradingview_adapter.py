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

    def __init__(self, name: str, result: ActionResult | None = None):
        self.name = name
        self.calls: list = []
        self._result = result

    async def execute(self, request):
        self.calls.append(request)
        return self._result or ActionResult(
            request_id=request.id, status=ActionStatus.SUCCEEDED, risk_level=RiskLevel.READ_ONLY,
            summary="ok", data={}, started_at=datetime.now(UTC),
        )


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


def _adapter(tv_result=None, trading_result=None, store=None) -> tuple[TradingViewAdapter, _FakeSkill, _FakeSkill]:
    tv = _FakeSkill("tradingview", tv_result)
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
                   "symbol": None, "timeframe": None}


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
