from __future__ import annotations

import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.action_contracts import (
    ActionRequest,
    ActionSource,
    ActionStatus,
    RiskLevel,
    SkillExecutionError,
    SkillValidationError,
)
from assistant.hot_chart_state import ChartIdentity, HotChartState
from skills.tradingview_control import TradingViewControlSkill

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="win32gui/uiautomation are Windows-only")


def _build_state(symbol: str, timeframe: str) -> HotChartState:
    now = datetime.now(UTC).isoformat()
    return HotChartState(
        identity=ChartIdentity(chart_window_id="555", symbol=symbol, timeframe=timeframe),
        analysis=SimpleNamespace(), screenshot_hash="deadbeef", source="vision",
        observed_at=now, analyzed_at=now,
    )


class _FakeStore:
    def __init__(self, state: HotChartState | None):
        self._state = state
        self.calls = 0

    async def get_latest_for_window(self, chart_window_id: str):
        self.calls += 1
        return self._state


def _request(action: str, arguments: dict) -> ActionRequest:
    return ActionRequest(skill="tradingview", action=action, arguments=arguments, source=ActionSource.hud)


def _ticking_monotonic():
    """A strictly increasing fake clock (0.5s per call) so both the title-poll (6s) and
    vision-poll (45s) deadlines are reached naturally after enough calls, regardless of exactly
    how many `time.monotonic()` calls each loop iteration makes."""
    t = 0.0
    while True:
        yield t
        t += 0.5


async def test_validate_set_symbol_requires_non_empty_symbol():
    skill = TradingViewControlSkill(_FakeStore(None))
    with pytest.raises(SkillValidationError):
        await skill.validate("set_symbol", {})
    await skill.validate("set_symbol", {"symbol": "EURUSD"})


async def test_validate_set_timeframe_requires_non_empty_timeframe():
    skill = TradingViewControlSkill(_FakeStore(None))
    with pytest.raises(SkillValidationError):
        await skill.validate("set_timeframe", {})
    await skill.validate("set_timeframe", {"timeframe": "15m"})


def test_classify_risk():
    skill = TradingViewControlSkill(_FakeStore(None))
    assert skill.classify_risk("status", {}) == RiskLevel.READ_ONLY
    assert skill.classify_risk("set_symbol", {}) == RiskLevel.LOW_RISK
    assert skill.classify_risk("set_timeframe", {}) == RiskLevel.LOW_RISK
    assert skill.classify_risk("delete_everything", {}) == RiskLevel.BLOCKED


async def test_status_reports_not_running_when_no_window():
    skill = TradingViewControlSkill(_FakeStore(None))
    with patch("skills.tradingview_control.resolve_window", side_effect=SkillExecutionError("no such window")):
        result = await skill.execute(_request("status", {}))
    assert result.status == ActionStatus.FAILED
    assert "not running" in result.summary.lower()


async def test_status_reports_symbol_and_timeframe_from_hot_chart_state():
    store = _FakeStore(_build_state("EURUSD", "15m"))
    skill = TradingViewControlSkill(store)
    with patch("skills.tradingview_control.resolve_window", return_value=(555, "TradingView.exe", "EURUSD")):
        result = await skill.execute(_request("status", {}))
    assert result.status == ActionStatus.SUCCEEDED
    assert result.risk_level == RiskLevel.READ_ONLY
    assert result.data["symbol"] == "EURUSD" and result.data["timeframe"] == "15m"


async def test_set_symbol_not_running_reports_not_running_outcome():
    skill = TradingViewControlSkill(_FakeStore(None))
    with patch("skills.tradingview_control.resolve_window", side_effect=SkillExecutionError("no such window")):
        result = await skill.execute(_request("set_symbol", {"symbol": "EURUSD"}))
    assert result.status == ActionStatus.FAILED
    assert result.data["outcome"] == "NOT_RUNNING"


async def test_set_symbol_verified_fast_via_window_title_change():
    """The common case: TradingView's own window title updates immediately (confirmed live on a
    real installed TradingView app), so verification never needs to wait on the slower vision
    pipeline."""
    skill = TradingViewControlSkill(_FakeStore(None))
    titles = iter(["XAUUSD", "XAUUSD", "EURUSD"])  # before, then title changes mid-poll
    with patch("skills.tradingview_control.resolve_window", return_value=(555, "TradingView.exe", "XAUUSD")), \
         patch("skills.tradingview_control.win32gui.GetWindowText", side_effect=lambda h: next(titles)), \
         patch("skills.tradingview_control._force_foreground"), \
         patch("skills.tradingview_control._click_to_focus"), \
         patch("skills.tradingview_control._send_keys") as mock_keys, \
         patch("skills.tradingview_control.time.sleep"):
        result = await skill.execute(_request("set_symbol", {"symbol": "eurusd"}))

    assert result.status == ActionStatus.SUCCEEDED
    assert result.data["outcome"] == "SUCCESS"
    mock_keys.assert_called_once_with("EURUSD{Enter}")


async def test_set_symbol_falls_back_to_vision_confirmation_when_title_never_changes():
    state = _build_state("EURUSD", "15m")
    store = _FakeStore(state)
    skill = TradingViewControlSkill(store)
    with patch("skills.tradingview_control.resolve_window", return_value=(555, "TradingView.exe", "XAUUSD")), \
         patch("skills.tradingview_control.win32gui.GetWindowText", return_value="XAUUSD"), \
         patch("skills.tradingview_control._force_foreground"), \
         patch("skills.tradingview_control._click_to_focus"), \
         patch("skills.tradingview_control._send_keys"), \
         patch("skills.tradingview_control.time.sleep"), \
         patch("asyncio.sleep", AsyncMock()), \
         patch("skills.tradingview_control.time.monotonic", side_effect=_ticking_monotonic()):
        result = await skill.execute(_request("set_symbol", {"symbol": "eurusd"}))

    assert result.status == ActionStatus.SUCCEEDED
    assert result.data["outcome"] == "SUCCESS"
    assert result.data["symbol"] == "EURUSD"


async def test_set_symbol_reports_not_verified_when_neither_signal_confirms():
    store = _FakeStore(None)  # no HotChartState ever appears
    skill = TradingViewControlSkill(store)
    with patch("skills.tradingview_control.resolve_window", return_value=(555, "TradingView.exe", "XAUUSD")), \
         patch("skills.tradingview_control.win32gui.GetWindowText", return_value="XAUUSD"), \
         patch("skills.tradingview_control._force_foreground"), \
         patch("skills.tradingview_control._click_to_focus"), \
         patch("skills.tradingview_control._send_keys"), \
         patch("skills.tradingview_control.time.sleep"), \
         patch("asyncio.sleep", AsyncMock()), \
         patch("skills.tradingview_control.time.monotonic", side_effect=_ticking_monotonic()):
        result = await skill.execute(_request("set_symbol", {"symbol": "eurusd"}))

    assert result.status == ActionStatus.FAILED
    assert result.data["outcome"] == "NOT_VERIFIED"


# ---- mission section 3: bounded retry + real mismatch detection (not "any change") ----

async def test_set_symbol_rejects_a_garbled_title_change_instead_of_false_success():
    """The live bug this guards against: the OLD check accepted ANY title
    change as success, so a garbled/incomplete type (observed live:
    'O' -> 'EURUS') was wrongly reported SUCCESS. Neither the title nor
    vision ever actually confirms EURUSD here, so this must end in
    NOT_VERIFIED after exhausting all bounded retries, never a false
    SUCCESS."""
    store = _FakeStore(None)
    skill = TradingViewControlSkill(store)
    with patch("skills.tradingview_control.resolve_window", return_value=(555, "TradingView.exe", "O")), \
         patch("skills.tradingview_control.win32gui.GetWindowText", return_value="EURUS"), \
         patch("skills.tradingview_control._force_foreground"), \
         patch("skills.tradingview_control._click_to_focus"), \
         patch("skills.tradingview_control._send_keys") as mock_keys, \
         patch("skills.tradingview_control.time.sleep"), \
         patch("asyncio.sleep", AsyncMock()), \
         patch("skills.tradingview_control.time.monotonic", side_effect=_ticking_monotonic()):
        result = await skill.execute(_request("set_symbol", {"symbol": "eurusd"}))

    assert result.status == ActionStatus.FAILED
    assert result.data["outcome"] == "NOT_VERIFIED"
    # All bounded attempts were actually used, not just one.
    assert mock_keys.call_count == 3


async def test_set_symbol_retries_and_succeeds_once_the_title_actually_matches():
    """First attempt types into a stale/garbled state (title never matches
    the request); second attempt lands correctly -- SUCCESS only once the
    title actually starts with the requested symbol."""
    attempt = {"n": 0}

    def fake_send_keys(_text):
        attempt["n"] += 1

    def fake_get_title(_hwnd):
        return "EURUSD rising fast" if attempt["n"] >= 2 else "EURUS"

    skill = TradingViewControlSkill(_FakeStore(None))
    with patch("skills.tradingview_control.resolve_window", return_value=(555, "TradingView.exe", "O")), \
         patch("skills.tradingview_control.win32gui.GetWindowText", side_effect=fake_get_title), \
         patch("skills.tradingview_control._force_foreground"), \
         patch("skills.tradingview_control._click_to_focus"), \
         patch("skills.tradingview_control._send_keys", side_effect=fake_send_keys), \
         patch("skills.tradingview_control.time.sleep"), \
         patch("asyncio.sleep", AsyncMock()), \
         patch("skills.tradingview_control.time.monotonic", side_effect=_ticking_monotonic()):
        result = await skill.execute(_request("set_symbol", {"symbol": "eurusd"}))

    assert result.status == ActionStatus.SUCCEEDED
    assert result.data["outcome"] == "SUCCESS"
    assert attempt["n"] == 2


async def test_set_timeframe_never_trusts_the_title_and_rejects_a_wrong_vision_read():
    """Timeframe never appears in the window title at all, so verification
    relies entirely on vision -- a vision read of the WRONG timeframe
    (e.g. still showing the previous 4h after asking for 15m) must not be
    accepted as success, and must exhaust bounded retries honestly."""
    store = _FakeStore(_build_state("EURUSD", "4h"))  # wrong: requested 15m
    skill = TradingViewControlSkill(store)
    with patch("skills.tradingview_control.resolve_window", return_value=(555, "TradingView.exe", "EURUSD")), \
         patch("skills.tradingview_control.win32gui.GetWindowText", return_value="EURUSD"), \
         patch("skills.tradingview_control._force_foreground"), \
         patch("skills.tradingview_control._click_to_focus"), \
         patch("skills.tradingview_control._send_keys") as mock_keys, \
         patch("skills.tradingview_control.time.sleep"), \
         patch("asyncio.sleep", AsyncMock()), \
         patch("skills.tradingview_control.time.monotonic", side_effect=_ticking_monotonic()):
        result = await skill.execute(_request("set_timeframe", {"timeframe": "15m"}))

    assert result.status == ActionStatus.FAILED
    assert result.data["outcome"] == "NOT_VERIFIED"
    assert mock_keys.call_count == 3


async def test_set_timeframe_retries_and_succeeds_once_vision_confirms_the_requested_value():
    attempt = {"n": 0}

    class _AttemptAwareStore:
        async def get_latest_for_window(self, chart_window_id):
            if attempt["n"] >= 2:
                return _build_state("EURUSD", "15m")
            return None  # first attempt: vision never confirms anything

    def fake_send_keys(_text):
        attempt["n"] += 1

    skill = TradingViewControlSkill(_AttemptAwareStore())
    with patch("skills.tradingview_control.resolve_window", return_value=(555, "TradingView.exe", "EURUSD")), \
         patch("skills.tradingview_control.win32gui.GetWindowText", return_value="EURUSD"), \
         patch("skills.tradingview_control._force_foreground"), \
         patch("skills.tradingview_control._click_to_focus"), \
         patch("skills.tradingview_control._send_keys", side_effect=fake_send_keys), \
         patch("skills.tradingview_control.time.sleep"), \
         patch("asyncio.sleep", AsyncMock()), \
         patch("skills.tradingview_control.time.monotonic", side_effect=_ticking_monotonic()):
        result = await skill.execute(_request("set_timeframe", {"timeframe": "15m"}))

    assert result.status == ActionStatus.SUCCEEDED
    assert result.data["outcome"] == "SUCCESS"
    assert result.data["timeframe"] == "15m"
    assert attempt["n"] == 2
