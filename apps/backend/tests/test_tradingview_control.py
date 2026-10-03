from __future__ import annotations

import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

import pytest

from app.action_contracts import (
    ActionRequest,
    ActionResult,
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
    # Each attempt sends a defensive {Esc} (dismiss any stray popup) before
    # the real keystroke -- see _change()'s reproduced-live comment.
    assert mock_keys.call_args_list == [call("{Esc}"), call("EURUSD{Enter}")]


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
    # All bounded attempts were actually used, not just one -- each sends a
    # defensive {Esc} followed by the real keystroke.
    real_keys = [c for c in mock_keys.call_args_list if c.args != ("{Esc}",)]
    assert len(real_keys) == 3


async def test_set_symbol_retries_and_succeeds_once_the_title_actually_matches():
    """First attempt types into a stale/garbled state (title never matches
    the request); second attempt lands correctly -- SUCCESS only once the
    title actually starts with the requested symbol."""
    attempt = {"n": 0}

    def fake_send_keys(text):
        if text != "{Esc}":  # only the real keystroke counts as an attempt
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
    real_keys = [c for c in mock_keys.call_args_list if c.args != ("{Esc}",)]
    assert len(real_keys) == 3


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


# ---- mission: fix the real timeframe control mechanism, not just verification ----
# Live investigation (capture + direct visual inspection of the real desktop app)
# found TradingView's "Change Interval" quick-entry dialog interprets a bare
# number as MINUTES (confirmed: "5" -> 5-minute chart, "60" -> 1-hour chart,
# both verified by screenshot) and does not recognize an "m"/"h" unit suffix
# appended to the number -- the previous code sent the literal "5m"/"1h" text,
# an invalid sequence for that field. Day/week/month ARE accepted with their
# letter suffix as-is (confirmed live: "1D" applies Daily).

from skills.tradingview_control import _tradingview_interval_keystroke


def test_minute_timeframes_convert_to_bare_minutes():
    assert _tradingview_interval_keystroke("5m") == "5"
    assert _tradingview_interval_keystroke("15m") == "15"
    assert _tradingview_interval_keystroke("1m") == "1"


def test_hour_timeframes_convert_to_minutes_equivalent():
    assert _tradingview_interval_keystroke("1h") == "60"
    assert _tradingview_interval_keystroke("4h") == "240"
    assert _tradingview_interval_keystroke("2h") == "120"


def test_case_insensitive_and_whitespace_tolerant():
    assert _tradingview_interval_keystroke("1H") == "60"
    assert _tradingview_interval_keystroke(" 5M ") == "5"


def test_day_week_month_pass_through_unchanged():
    assert _tradingview_interval_keystroke("1D") == "1D"
    assert _tradingview_interval_keystroke("1W") == "1W"
    assert _tradingview_interval_keystroke("D") == "D"


def test_unrecognized_pattern_passes_through_unchanged():
    assert _tradingview_interval_keystroke("whatever") == "whatever"


async def test_set_timeframe_sends_the_converted_minutes_keystroke_not_the_literal_suffix():
    """The actual fix under test: set_timeframe("1h") must send TradingView
    the bare "60", never the literal "1h" -- regression guard for the real
    live bug (sending "1h"/"5m" literally is not valid numeric input for
    the Change Interval dialog). No HotChartState is wired in this fake, so
    verification never confirms and the bounded retry fires more than
    once -- this only asserts on the keystroke text each attempt actually
    sends, which must be the converted form every time."""
    skill = TradingViewControlSkill(_FakeStore(None))
    with patch("skills.tradingview_control.resolve_window", return_value=(555, "TradingView.exe", "XAUUSD")), \
         patch("skills.tradingview_control.win32gui.GetWindowText", return_value="XAUUSD"), \
         patch("skills.tradingview_control._force_foreground"), \
         patch("skills.tradingview_control._click_to_focus"), \
         patch("skills.tradingview_control._send_keys") as mock_keys, \
         patch("skills.tradingview_control.time.sleep"), \
         patch("asyncio.sleep", AsyncMock()), \
         patch("skills.tradingview_control.time.monotonic", side_effect=_ticking_monotonic()):
        await skill.execute(_request("set_timeframe", {"timeframe": "1h"}))

    real_keys = [c for c in mock_keys.call_args_list if c.args != ("{Esc}",)]
    assert len(real_keys) >= 1
    assert all(c.args == ("60{Enter}",) for c in real_keys)


async def test_set_timeframe_sends_bare_digits_for_minute_intervals():
    skill = TradingViewControlSkill(_FakeStore(None))
    with patch("skills.tradingview_control.resolve_window", return_value=(555, "TradingView.exe", "XAUUSD")), \
         patch("skills.tradingview_control.win32gui.GetWindowText", return_value="XAUUSD"), \
         patch("skills.tradingview_control._force_foreground"), \
         patch("skills.tradingview_control._click_to_focus"), \
         patch("skills.tradingview_control._send_keys") as mock_keys, \
         patch("skills.tradingview_control.time.sleep"), \
         patch("asyncio.sleep", AsyncMock()), \
         patch("skills.tradingview_control.time.monotonic", side_effect=_ticking_monotonic()):
        await skill.execute(_request("set_timeframe", {"timeframe": "15m"}))

    # First attempt's real keystroke is what matters for this regression
    # guard (index 0 is the defensive {Esc}); verification itself is
    # covered by the other timeframe tests above.
    assert mock_keys.call_args_list[1].args == ("15{Enter}",)


def test_click_to_focus_avoids_the_price_axis_instead_of_clicking_dead_center():
    """Reproduced live: the render child is one Chromium surface spanning
    the whole app (toolbar, chart, watchlist), so its geometric center
    lands almost exactly on the chart's own price axis -- a single click
    there opens TradingView's "quick trade at this price" popup, which then
    gets stuck open and blocks every later click/keystroke (confirmed by a
    screenshot of that exact menu after an unattended automated run). The
    click must land inside the plain candle area instead, clear of center."""
    from skills.tradingview_control import _click_to_focus

    with patch("skills.tradingview_control._find_render_child", return_value=999), \
         patch("skills.tradingview_control.win32gui.GetWindowRect", return_value=(800, 0, 1900, 1000)), \
         patch("skills.tradingview_control.win32api.SetCursorPos") as mock_pos, \
         patch("skills.tradingview_control.win32api.mouse_event"):
        _click_to_focus(555)

    (cx, cy), = mock_pos.call_args.args
    width, height = 1900 - 800, 1000 - 0
    center_x, center_y = 800 + width // 2, height // 2
    assert cx != center_x and cy != center_y
    # Clearly inside the left/upper portion -- away from the right-edge
    # price axis and the top toolbar.
    assert cx < 800 + width * 0.4
    assert cy < height * 0.6


def _bmp_data_uri() -> str:
    import base64
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color=(10, 20, 30)).save(buf, format="BMP")
    return "data:image/bmp;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


class _FakeCaptureSkill:
    """Stand-in for the windows_app skill _DirectSkillRuntime calls
    directly (bypassing ActionRuntime -- see _active_vision_confirm)."""

    def __init__(self, *, window_title="EURUSD - TradingView"):
        self._window_title = window_title
        self.calls = 0

    async def execute(self, request):
        self.calls += 1
        return ActionResult(
            request_id=request.id, status=ActionStatus.SUCCEEDED, risk_level=RiskLevel.READ_ONLY,
            summary="captured",
            data={"executable": "TradingView.exe", "window_title": self._window_title,
                 "image_data_base64": _bmp_data_uri(), "image_format": "image/bmp",
                 "is_secure_desktop": False, "error": None},
        )


class _FakeChartAnalysisService:
    def __init__(self, *, instrument, timeframe):
        self._instrument = instrument
        self._timeframe = timeframe
        self.calls = 0

    async def analyze(self, **_kwargs):
        self.calls += 1
        return SimpleNamespace(instrument=self._instrument, timeframe=self._timeframe)


async def test_set_timeframe_uses_active_capture_and_analyze_when_wired():
    """Reproduced live (mission: harden TradingView control/capture
    reliability): the ambient background watcher only produced 3 fresh
    vision reads across a 7-minute, 8-call live timeframe-switch test --
    far too sparse for _wait_for_vision_confirmation's passive poll to
    reliably catch a match within any single bounded attempt. When a
    chart_analysis_service/capture_skill are wired (skills/registry.py),
    verification must instead do a fresh, deterministic capture+analyze
    tied to this specific attempt, never touching the passive store."""
    store = _FakeStore(None)  # must never be consulted on this path
    capture_skill = _FakeCaptureSkill()
    analysis = _FakeChartAnalysisService(instrument="EURUSD", timeframe="15m")
    skill = TradingViewControlSkill(store, chart_analysis_service=analysis, capture_skill=capture_skill)

    with patch("skills.tradingview_control.resolve_window", return_value=(555, "TradingView.exe", "EURUSD")), \
         patch("skills.tradingview_control.win32gui.GetWindowText", return_value="EURUSD"), \
         patch("skills.tradingview_control._force_foreground"), \
         patch("skills.tradingview_control._click_to_focus"), \
         patch("skills.tradingview_control._send_keys"), \
         patch("skills.tradingview_control.time.sleep"), \
         patch("asyncio.sleep", AsyncMock()):
        result = await skill.execute(_request("set_timeframe", {"timeframe": "15m"}))

    assert result.status == ActionStatus.SUCCEEDED
    assert result.data["outcome"] == "SUCCESS"
    assert result.data["timeframe"] == "15m"
    assert capture_skill.calls == 1
    assert analysis.calls == 1
    assert store.calls == 0  # the ambient watcher's store was never consulted


async def test_active_vision_confirm_rejects_a_mismatched_read_and_exhausts_bounded_retries():
    store = _FakeStore(None)
    capture_skill = _FakeCaptureSkill()
    # Vision genuinely sees a different timeframe than requested -- a true
    # negative, never accepted as success.
    analysis = _FakeChartAnalysisService(instrument="EURUSD", timeframe="4h")
    skill = TradingViewControlSkill(store, chart_analysis_service=analysis, capture_skill=capture_skill)

    with patch("skills.tradingview_control.resolve_window", return_value=(555, "TradingView.exe", "EURUSD")), \
         patch("skills.tradingview_control.win32gui.GetWindowText", return_value="EURUSD"), \
         patch("skills.tradingview_control._force_foreground"), \
         patch("skills.tradingview_control._click_to_focus"), \
         patch("skills.tradingview_control._send_keys"), \
         patch("skills.tradingview_control.time.sleep"), \
         patch("asyncio.sleep", AsyncMock()):
        result = await skill.execute(_request("set_timeframe", {"timeframe": "15m"}))

    assert result.status == ActionStatus.FAILED
    assert result.data["outcome"] == "NOT_VERIFIED"
    assert capture_skill.calls == 3  # all bounded attempts actually used
    assert store.calls == 0


async def test_set_symbol_and_set_timeframe_get_a_longer_execution_timeout_than_the_runtime_default():
    """Reproduced live (mission: harden TradingView control/capture
    reliability): the ActionRuntime's generic 30s dispatch timeout cut off
    set_timeframe mid vision-verification, returning FAILED with no data
    well before the bounded retry/verify contract could honestly finish.
    Both mutating actions must ask for more than that generic default;
    every read-only action must stay on it (None = "use the default")."""
    skill = TradingViewControlSkill(_FakeStore(None))

    assert skill.execution_timeout_for("set_symbol") > 30.0
    assert skill.execution_timeout_for("set_timeframe") > 30.0
    assert skill.execution_timeout_for("status") is None
    assert skill.execution_timeout_for("focus") is None
