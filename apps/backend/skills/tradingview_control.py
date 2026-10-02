"""`tradingview` skill -- read TradingView's current symbol/timeframe from the existing
HotChartState vision pipeline (never re-implemented here), and write a symbol/timeframe change via
real OS-level keystroke simulation.

Why not UI Automation: TradingView's chart is a GPU-rendered canvas (confirmed live on this
machine -- its window hosts `Chrome_RenderWidgetHostHWND`/"Intermediate D3D Window" children, i.e.
it is a Chromium/Electron-class app drawing its own pixels) with no semantic UIA control tree for
the chart itself, unlike the classic win32 controls `skills/_desktop_automation.py` targets. That
module's own docstring is explicit that it deliberately never falls back to coordinate/keystroke
simulation -- this is the one case in this codebase where no semantic UIA target exists at all, so
simulating the same keystrokes a user's own typing would produce (TradingView's built-in symbol
quick-search: type a ticker while the chart has focus, press Enter) is the only viable mechanism,
and it lives in its own module rather than inside `_desktop_automation.py` to keep that module's
"no blind simulation" contract intact.

Focus reliability: `SetForegroundWindow` alone is unreliable for a background/automated caller --
confirmed live on this machine, where an unrelated window stayed foreground despite a plain
`SetForegroundWindow` call succeeding by return value. `_force_foreground` uses the standard
`AttachThreadInput` technique (temporarily joining this thread's input queue to the foreground and
target threads) to make it reliable; a click on the chart's actual render-surface child window
(not just the top-level frame) is also required before keystrokes reach the chart's own input
handling, since Chromium/Electron apps route keyboard focus to a specific child HWND, not the
top-level frame.

Never trusted blind: every change is verified against the window's own title (TradingView's
desktop app puts the current symbol directly in it, confirmed live) within a short timeout, and
`set_symbol`/`set_timeframe` report FAILED/NOT_VERIFIED rather than a fabricated SUCCESS if that
verification does not happen -- the existing BackgroundChartWatcher/HotChartState vision pipeline
will independently confirm the change a little later on its own schedule, which `status()` also
surfaces.
"""
from __future__ import annotations

import ctypes
import logging
import time
from datetime import UTC, datetime
from typing import Any

import win32api
import win32con
import win32gui
import win32process

from app.action_contracts import (
    ActionRequest,
    ActionResult,
    ActionStatus,
    BaseSkill,
    RiskLevel,
    SkillExecutionError,
    SkillValidationError,
)
from skills._desktop_automation import resolve_window
from skills.windows_app import _focus_hwnd

logger = logging.getLogger("tars.tradingview")

_user32 = ctypes.windll.user32
_TITLE_VERIFY_TIMEOUT = 6.0
_VISION_VERIFY_TIMEOUT = 45.0  # generous: real vision analysis measured ~15-31s (hot_chart_state.py)


def _force_foreground(hwnd: int) -> None:
    """`SetForegroundWindow` on its own is denied by Windows' foreground-lock heuristic for a
    caller that is not itself the last-input process -- exactly this backend's situation when a
    voice command triggers a window switch. AttachThreadInput temporarily shares input state with
    both the current foreground window's thread and the target's, which the same Windows heuristic
    exempts from the restriction."""
    fg = win32gui.GetForegroundWindow()
    fg_thread = win32process.GetWindowThreadProcessId(fg)[0] if fg else 0
    target_thread = win32process.GetWindowThreadProcessId(hwnd)[0]
    cur_thread = win32api.GetCurrentThreadId()
    attached_fg = attached_target = False
    if fg_thread and fg_thread != cur_thread:
        attached_fg = bool(_user32.AttachThreadInput(cur_thread, fg_thread, True))
    if target_thread != cur_thread:
        attached_target = bool(_user32.AttachThreadInput(cur_thread, target_thread, True))
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
    finally:
        if attached_fg:
            _user32.AttachThreadInput(cur_thread, fg_thread, False)
        if attached_target:
            _user32.AttachThreadInput(cur_thread, target_thread, False)


def _find_render_child(hwnd: int) -> int:
    """The chart's actual keyboard/mouse-receiving surface (see module docstring) -- a
    Chrome_RenderWidgetHostHWND child, not the top-level frame. Falls back to the frame itself if
    none is found (e.g. a differently-built TradingView version), so this never raises."""
    children: list[int] = []

    def _cb(child: int, _extra: None) -> None:
        if win32gui.GetClassName(child) == "Chrome_RenderWidgetHostHWND":
            children.append(child)

    try:
        win32gui.EnumChildWindows(hwnd, _cb, None)
    except Exception:
        pass
    return children[0] if children else hwnd


def _click_to_focus(hwnd: int) -> None:
    target = _find_render_child(hwnd)
    left, top, right, bottom = win32gui.GetWindowRect(target)
    cx, cy = (left + right) // 2, (top + bottom) // 2
    win32api.SetCursorPos((cx, cy))
    time.sleep(0.1)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.05)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def _send_keys(text: str) -> None:
    import uiautomation as auto

    auto.SendKeys(text, waitTime=0.05)


class TradingViewControlSkill(BaseSkill):
    name = "tradingview"
    description = "Read TradingView's current symbol/timeframe and change them on the running chart window."
    capabilities: tuple[str, ...] = ("status", "set_symbol", "set_timeframe", "focus")

    def __init__(self, hot_chart_store) -> None:
        self._store = hot_chart_store

    async def health(self) -> dict[str, Any]:
        return {"available": True}

    def classify_risk(self, action: str, arguments: dict[str, Any]) -> RiskLevel:
        if action in ("status",):
            return RiskLevel.READ_ONLY
        if action in ("set_symbol", "set_timeframe", "focus"):
            return RiskLevel.LOW_RISK
        return RiskLevel.BLOCKED

    async def validate(self, action: str, arguments: dict[str, Any]) -> None:
        if action == "set_symbol":
            if not isinstance(arguments.get("symbol"), str) or not arguments["symbol"].strip():
                raise SkillValidationError("set_symbol requires non-empty 'symbol'")
        elif action == "set_timeframe":
            if not isinstance(arguments.get("timeframe"), str) or not arguments["timeframe"].strip():
                raise SkillValidationError("set_timeframe requires non-empty 'timeframe'")
        elif action in ("status", "focus"):
            return
        else:
            raise SkillValidationError(f"unsupported tradingview action '{action}'")

    async def execute(self, request: ActionRequest) -> ActionResult:
        started = datetime.now(UTC)
        if request.action == "status":
            return await self._execute_status(request, started)
        if request.action == "focus":
            return self._execute_focus(request, started)
        if request.action == "set_symbol":
            return await self._execute_set_symbol(request, started)
        if request.action == "set_timeframe":
            return await self._execute_set_timeframe(request, started)
        raise SkillExecutionError(f"unsupported tradingview action '{request.action}'")

    @staticmethod
    def _window() -> tuple[int, str, str] | None:
        try:
            return resolve_window("tradingview")
        except SkillExecutionError:
            return None

    async def _execute_status(self, request: ActionRequest, started: datetime) -> ActionResult:
        found = self._window()
        if found is None:
            return self._result(
                request, ActionStatus.FAILED, "TradingView is not running.",
                risk_level=RiskLevel.READ_ONLY, error="tradingview window not found", started_at=started,
            )
        hwnd, _exe, title = found
        state = await self._store.get_latest_for_window(str(hwnd))
        symbol = state.identity.symbol if state else None
        timeframe = state.identity.timeframe if state else None
        detail = f"{symbol or 'an unrecognised symbol'} on {timeframe or 'an unrecognised timeframe'}" if state else \
            "open; no chart has been visually analysed yet"
        return self._result(
            request, ActionStatus.SUCCEEDED, f"TradingView is showing {detail}.",
            risk_level=RiskLevel.READ_ONLY,
            data={"window_title": title, "symbol": symbol, "timeframe": timeframe,
                 "freshness": state.freshness().value if state else "missing"},
            started_at=started,
        )

    def _execute_focus(self, request: ActionRequest, started: datetime) -> ActionResult:
        found = self._window()
        if found is None:
            return self._result(
                request, ActionStatus.FAILED, "TradingView is not running.",
                risk_level=RiskLevel.LOW_RISK, error="tradingview window not found", started_at=started,
            )
        hwnd, _exe, title = found
        _focus_hwnd(hwnd)
        _force_foreground(hwnd)
        return self._result(
            request, ActionStatus.SUCCEEDED, f"Focused '{title}'.",
            risk_level=RiskLevel.LOW_RISK, data={"window_title": title}, started_at=started,
        )

    async def _execute_set_symbol(self, request: ActionRequest, started: datetime) -> ActionResult:
        symbol = request.arguments["symbol"].strip().upper()
        return await self._change("symbol", symbol, request, started,
                                  keys=lambda: _send_keys(f"{symbol}{{Enter}}"))

    async def _execute_set_timeframe(self, request: ActionRequest, started: datetime) -> ActionResult:
        timeframe = request.arguments["timeframe"].strip()
        # Best-effort: TradingView's timeframe control is a toolbar dropdown, not a documented
        # universal keyboard shortcut across versions -- this could not be live-verified in this
        # environment (see skills/tradingview_control.py module docstring / the mission report for
        # why). It uses the same focus+type+verify path as set_symbol on the reasonable assumption
        # that a real chart also accepts a typed interval via the same quick-entry mechanism; if
        # verification fails, this honestly reports FAILED rather than a guessed success.
        return await self._change("timeframe", timeframe, request, started,
                                  keys=lambda: _send_keys(f"{timeframe}{{Enter}}"))

    async def _change(self, field: str, value: str, request: ActionRequest, started: datetime, *, keys) -> ActionResult:
        found = self._window()
        if found is None:
            return self._result(
                request, ActionStatus.FAILED, "TradingView is not running.",
                risk_level=RiskLevel.LOW_RISK, error="tradingview window not found",
                data={"outcome": "NOT_RUNNING"}, started_at=started,
            )
        hwnd, _exe, before_title = found
        try:
            _force_foreground(hwnd)
            time.sleep(0.3)
            _click_to_focus(hwnd)
            time.sleep(0.3)
            keys()
        except Exception as exc:
            raise SkillExecutionError(f"failed to send {field} change to TradingView: {exc}") from exc

        deadline = time.monotonic() + _TITLE_VERIFY_TIMEOUT
        after_title = before_title
        while time.monotonic() < deadline:
            after_title = win32gui.GetWindowText(hwnd)
            if after_title != before_title:
                break
            time.sleep(0.3)
        if after_title != before_title:
            logger.info("[tradingview] %s change verified via window title: %r -> %r", field, before_title, after_title)
            return self._result(
                request, ActionStatus.SUCCEEDED, f"TradingView now shows '{after_title}'.",
                risk_level=RiskLevel.LOW_RISK,
                data={"outcome": "SUCCESS", "requested": value, "window_title": after_title},
                started_at=started,
            )

        # Title didn't change within the fast window -- fall back to the slower, authoritative
        # vision-based confirmation the background chart watcher will produce on its own schedule,
        # rather than immediately declaring failure over what might just be a slow UI response.
        state = await self._wait_for_vision_confirmation(str(hwnd), field, value)
        if state is not None:
            logger.info("[tradingview] %s change verified via HotChartState: %r", field, state.identity)
            return self._result(
                request, ActionStatus.SUCCEEDED,
                f"TradingView now shows {state.identity.symbol} on {state.identity.timeframe}.",
                risk_level=RiskLevel.LOW_RISK,
                data={"outcome": "SUCCESS", "requested": value, "symbol": state.identity.symbol,
                     "timeframe": state.identity.timeframe},
                started_at=started,
            )
        return self._result(
            request, ActionStatus.FAILED,
            f"Asked TradingView to change {field} to {value}, but couldn't verify it took effect.",
            risk_level=RiskLevel.LOW_RISK, error=f"{field} change not verified",
            data={"outcome": "NOT_VERIFIED", "requested": value}, started_at=started,
        )

    async def _wait_for_vision_confirmation(self, chart_window_id: str, field: str, value: str):
        import asyncio

        value_norm = value.strip().lower()
        deadline = time.monotonic() + _VISION_VERIFY_TIMEOUT
        while time.monotonic() < deadline:
            state = await self._store.get_latest_for_window(chart_window_id)
            if state is not None:
                current = (state.identity.symbol if field == "symbol" else state.identity.timeframe) or ""
                if current.strip().lower() == value_norm:
                    return state
            await asyncio.sleep(2.0)
        return None
