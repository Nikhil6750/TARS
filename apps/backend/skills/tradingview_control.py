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

import logging
import re
import time
from datetime import UTC, datetime
from typing import Any

import win32api
import win32con
import win32gui

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
from skills.windows_app import _activate_and_verify_foreground, _force_foreground

logger = logging.getLogger("tars.tradingview")

_TITLE_VERIFY_TIMEOUT = 6.0
_VISION_VERIFY_TIMEOUT = 45.0  # generous: real vision analysis measured ~15-31s (hot_chart_state.py)
# Mission section 3 (hardened symbol/timeframe switching): one initial
# attempt plus up to two retries -- live testing found the keystroke
# simulation occasionally typing into the wrong control state (a stale
# autocomplete popup, a half-focused search box), landing on a garbled or
# unrelated symbol. A bounded retry (re-focus, re-click, re-type) resolves
# that without ever reporting SUCCESS on an unverified guess.
_MAX_ATTEMPTS = 3
# Reproduced live (mission: harden TradingView control/capture reliability):
# the generic ActionRuntime dispatch timeout (30s) cut off set_timeframe mid-
# verification -- every timeframe change falls through to vision confirmation
# (_VISION_VERIFY_TIMEOUT=45s per attempt), and a symbol change that misses
# the fast title match falls through to the same vision wait after its own
# 6s title-poll window, so one single _change() attempt alone can legitimately
# take longer than the generic default, well before _MAX_ATTEMPTS is
# exhausted. Sized to comfortably cover the worst case of all _MAX_ATTEMPTS
# attempts each needing the full vision wait, not merely the common case.
_MUTATION_EXECUTION_TIMEOUT = _MAX_ATTEMPTS * (_TITLE_VERIFY_TIMEOUT + _VISION_VERIFY_TIMEOUT) + 15.0
# Brief settle time before an active verification capture, so the just-sent
# Enter keypress's dialog has visibly closed/redrawn (same justification as
# market_explainer.py's _SYMBOL_SETTLE_SECONDS).
_VERIFY_SETTLE_SECONDS = 1.5


class _DirectSkillRuntime:
    """Minimal action_runtime-shaped adapter (just `.submit()`) so
    capture_and_analyze_chart (assistant/chart_capture.py) can be reused
    here without going through the real ActionRuntime -- see
    TradingViewControlSkill._active_vision_confirm for why that would
    deadlock. Calls the windows_app skill's execute() directly instead,
    the same bypass skills/trading.py's _dispatch_capture already uses
    (via a frontend bridge) for the same reason."""

    def __init__(self, capture_skill: Any) -> None:
        self._skill = capture_skill

    async def submit(self, request: ActionRequest) -> ActionResult:
        return await self._skill.execute(request)


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
    """Reproduced live (mission: harden TradingView control/capture
    reliability -- investigate before fixing): the render child is ONE
    Chromium surface spanning the entire app content (toolbar, chart,
    watchlist), not just the chart canvas -- confirmed by GetWindowRect
    matching the full window. Its geometric CENTER lands almost exactly on
    the chart's own right-side price axis (measured live: center at ~50%
    width, same column as the price labels), where a single click opens
    TradingView's "quick trade at this price" popup (Add alert/Buy/Sell/
    Add order/Draw line) -- confirmed by a screenshot showing that exact
    menu stuck open after an unattended automated run, blocking every
    subsequent click/keystroke until dismissed. Clicking at 25% width /
    45% height instead lands inside the plain candlestick plot area,
    comfortably clear of that price-axis column, the left drawing-tools
    sidebar, and the top toolbar."""
    target = _find_render_child(hwnd)
    left, top, right, bottom = win32gui.GetWindowRect(target)
    cx = left + int((right - left) * 0.25)
    cy = top + int((bottom - top) * 0.45)
    win32api.SetCursorPos((cx, cy))
    time.sleep(0.1)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.05)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def _send_keys(text: str) -> None:
    import uiautomation as auto

    auto.SendKeys(text, waitTime=0.05)


_TIMEFRAME_ENTRY_PATTERN = re.compile(r"^\s*(\d+)\s*([mMhH])\s*$")


def _tradingview_interval_keystroke(timeframe: str) -> str:
    """Converts a canonical timeframe string ("5m", "1h", "4h") into the
    literal text TradingView's own "Change Interval" quick-entry dialog
    actually accepts.

    Confirmed live against the real desktop app (mission: harden
    TradingView control, investigate before fixing): typing a bare digit
    while the chart has keyboard focus opens a "Change Interval" overlay
    that interprets the number as MINUTES, with a live preview label
    ("5 minutes") confirming it before Enter applies it -- typing "5"
    applies a 5-minute chart, typing "60" applies 1-hour, both verified by
    screenshot. The dialog does NOT recognize an "m"/"h" unit suffix
    appended to the number: sending the previous literal "5m" typed the
    digit "5" (opening the overlay) followed by the character "m" (not
    valid numeric input), an unreliable sequence -- this is the exact
    mechanism live acceptance testing traced minute-timeframe-switch
    failures to, not a verification bug (verification was already
    correct; see hardened retry/match logic elsewhere in this file).
    Day/week/month values ARE accepted by the same dialog with their
    letter suffix as literal text (confirmed live: "1D" applies Daily),
    so anything that is not a bare N+m/h pattern passes through
    unchanged."""
    match = _TIMEFRAME_ENTRY_PATTERN.match(timeframe)
    if not match:
        return timeframe
    value, unit = int(match.group(1)), match.group(2).lower()
    if unit == "m":
        return str(value)
    if unit == "h":
        return str(value * 60)
    return timeframe


class TradingViewControlSkill(BaseSkill):
    name = "tradingview"
    description = "Read TradingView's current symbol/timeframe and change them on the running chart window."
    capabilities: tuple[str, ...] = ("status", "set_symbol", "set_timeframe", "focus")

    def __init__(self, hot_chart_store, *, chart_analysis_service=None, capture_skill=None) -> None:
        self._store = hot_chart_store
        # Optional: wire these (see skills/registry.py) to verify a
        # symbol/timeframe change with a fresh, deterministic, action-tied
        # capture+vision-analyze instead of passively waiting on the
        # ambient background watcher (assistant/chart_watch.py +
        # chart_watcher.rs). Reproduced live (mission: harden TradingView
        # control/capture reliability): the ambient watcher's own hash-diff
        # change detection, idle-pause, and cooldown -- tuned for passive
        # monitoring, not "I just changed something, confirm it now" --
        # produced only 3 fresh vision reads across a 7-minute, 8-call live
        # timeframe-switch test, starving _wait_for_vision_confirmation of
        # a fresh read within any single bounded attempt. None/None (the
        # default) keeps the old passive-only path, e.g. for tests that
        # don't wire these.
        self._chart_analysis = chart_analysis_service
        self._capture_skill = capture_skill
        # Structured/control-verified identity (mission section 2: vision
        # must never be the authoritative source for symbol/timeframe when
        # TARS itself just deterministically set and verified one). Set
        # only on a confirmed _change() success -- the exact value TARS
        # asked for and actually confirmed, never a vision guess. Each
        # field is independent: changing the symbol does not clear a
        # previously-verified timeframe, and vice versa. Session-scoped
        # (in-memory, per backend process) -- a restart returns to "no
        # verified identity yet," correctly falling back to vision.
        self._verified_symbol: str | None = None
        self._verified_timeframe: str | None = None
        self._verified_at: str | None = None

    def verified_identity(self) -> dict[str, str | None]:
        """The last deterministically-set-and-confirmed symbol/timeframe,
        if any -- see TradingViewAdapter.monitor_chart() for how this
        outranks a HotChartState vision read."""
        return {
            "symbol": self._verified_symbol,
            "timeframe": self._verified_timeframe,
            "verified_at": self._verified_at,
        }

    def _record_verified(self, field: str, value: str) -> None:
        if field == "symbol":
            self._verified_symbol = value.strip().upper()
        elif field == "timeframe":
            self._verified_timeframe = value.strip()
        self._verified_at = datetime.now(UTC).isoformat()

    def execution_timeout_for(self, action: str) -> float | None:
        if action in ("set_symbol", "set_timeframe"):
            return _MUTATION_EXECUTION_TIMEOUT
        return None

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
        # Mission: P0 regression -- same unverified-foreground bug class as
        # windows_app.py's launch/focus (an API call returning without an
        # exception is not proof the window actually became foreground
        # under Windows' foreground-lock heuristic). Isolated to this
        # simple focus action only; _change()'s own retry/vision-verify
        # loop (symbol/timeframe switching) is untouched.
        if not _activate_and_verify_foreground(hwnd):
            return self._result(
                request, ActionStatus.FAILED, f"'{title}' could not be brought to the foreground.",
                risk_level=RiskLevel.LOW_RISK, error="foreground activation of TradingView was not verified",
                data={"outcome": "FOREGROUND_VERIFICATION_FAILED", "window_title": title}, started_at=started,
            )
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
        entry_text = _tradingview_interval_keystroke(timeframe)
        # `value` passed to _change is the ORIGINAL canonical string
        # ("1h"/"5m") -- verification compares against this (title/vision
        # both report the canonical form); only the literal keystroke text
        # is the converted one the dialog actually accepts.
        return await self._change("timeframe", timeframe, request, started,
                                  keys=lambda: _send_keys(f"{entry_text}{{Enter}}"))

    async def _change(self, field: str, value: str, request: ActionRequest, started: datetime, *, keys) -> ActionResult:
        """Deterministic action + verify-the-actual-result + bounded retry
        (mission: "Never report SUCCESS merely because keystrokes were
        sent"). Live testing found the previous title-based check reporting
        SUCCESS whenever the title merely CHANGED (e.g. "O" -> "EURUS", a
        garbled/incomplete type) rather than checking it changed to the
        REQUESTED value -- _title_shows_symbol now requires an actual
        match. Up to `_MAX_ATTEMPTS` full attempts (re-focus, re-click,
        re-type, re-verify) before honestly reporting NOT_VERIFIED; a wrong
        symbol/timeframe is never left silently active without it."""
        found = self._window()
        if found is None:
            return self._result(
                request, ActionStatus.FAILED, "TradingView is not running.",
                risk_level=RiskLevel.LOW_RISK, error="tradingview window not found",
                data={"outcome": "NOT_RUNNING"}, started_at=started,
            )
        hwnd, _exe, _title = found

        last_summary = f"Asked TradingView to change {field} to {value}, but couldn't verify it took effect."
        last_data: dict[str, Any] = {"requested": value}
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                _force_foreground(hwnd)
                time.sleep(0.3)
                # Defensive dismiss: reproduced live, a stray click (see
                # _click_to_focus) can leave TradingView's price-axis
                # "quick trade at this price" popup open, which then
                # intercepts the next attempt's click/keystrokes. Escape is
                # a no-op when nothing is open, so this never costs a
                # working case anything beyond the one short key send.
                _send_keys("{Esc}")
                time.sleep(0.2)
                _click_to_focus(hwnd)
                time.sleep(0.3)
                keys()
            except Exception as exc:
                raise SkillExecutionError(f"failed to send {field} change to TradingView: {exc}") from exc

            verified, summary, data = await self._verify_change(hwnd, field, value)
            if verified:
                logger.info("[tradingview] %s change verified on attempt %d/%d: %r",
                            field, attempt, _MAX_ATTEMPTS, data)
                self._record_verified(field, value)
                return self._result(
                    request, ActionStatus.SUCCEEDED, summary, risk_level=RiskLevel.LOW_RISK,
                    data={**data, "outcome": "SUCCESS", "requested": value}, started_at=started,
                )
            last_summary, last_data = summary, data
            logger.info("[tradingview] %s change attempt %d/%d not verified (requested %r, saw %r)",
                        field, attempt, _MAX_ATTEMPTS, value, data)

        return self._result(
            request, ActionStatus.FAILED, last_summary,
            risk_level=RiskLevel.LOW_RISK, error=f"{field} change not verified after {_MAX_ATTEMPTS} attempts",
            data={**last_data, "outcome": "NOT_VERIFIED", "requested": value}, started_at=started,
        )

    async def _verify_change(self, hwnd: int, field: str, value: str) -> tuple[bool, str, dict[str, Any]]:
        """One verification pass for one `_change` attempt. Symbol changes
        get a fast path via the window title (TradingView's desktop app
        puts the current symbol directly in it, confirmed live) -- but
        only counts a match that actually STARTS WITH the requested symbol,
        never merely "the title is different from before" (that accepted
        a garbled/wrong type as success). Timeframe never appears in the
        title at all, so it always falls through to vision."""
        if field == "symbol":
            deadline = time.monotonic() + _TITLE_VERIFY_TIMEOUT
            target = value.strip().upper()
            while time.monotonic() < deadline:
                title = win32gui.GetWindowText(hwnd)
                if title.strip().upper().startswith(target):
                    return True, f"TradingView now shows '{title}'.", {"window_title": title}
                time.sleep(0.3)

        # Title path didn't confirm (or doesn't apply to timeframe). Prefer an
        # active, deterministic capture+analyze tied to THIS attempt over
        # passively waiting on the ambient background watcher -- see
        # __init__'s reproduced-live comment on why the ambient path alone
        # is unreliable here. Falls back to the ambient path when the active
        # dependencies aren't wired (e.g. tests).
        if self._chart_analysis is not None and self._capture_skill is not None:
            return await self._active_vision_confirm(hwnd, field, value)

        state = await self._wait_for_vision_confirmation(str(hwnd), field, value)
        if state is not None:
            return (
                True,
                f"TradingView now shows {state.identity.symbol} on {state.identity.timeframe}.",
                {"symbol": state.identity.symbol, "timeframe": state.identity.timeframe},
            )
        return False, f"{field} change to '{value}' was not confirmed.", {}

    async def _active_vision_confirm(self, hwnd: int, field: str, value: str) -> tuple[bool, str, dict[str, Any]]:
        """Fresh, synchronous capture -> vision-analyze -> compare, tied
        directly to this attempt -- never reads a cache, never waits on the
        ambient watcher's own schedule. Reuses the same capture/verify/
        decode primitive the Universal Market Explainer and the single-shot
        "analyze this chart" path already use (assistant/chart_capture.py),
        via a direct skill.execute() call rather than ActionRuntime.submit()
        -- going through the real runtime here would deadlock, since this
        code already runs inside an in-flight action holding the runtime's
        own lock (actions/runtime.py's `async with self._lock`)."""
        import asyncio

        from assistant.chart_capture import capture_and_analyze_chart

        await asyncio.sleep(_VERIFY_SETTLE_SECONDS)
        outcome = await capture_and_analyze_chart(
            _DirectSkillRuntime(self._capture_skill), self._chart_analysis,
            goal_text="What symbol and timeframe is this chart currently showing?",
            conversation_id=f"tradingview-control-verify:{hwnd}",
        )
        if not outcome.analyzed or outcome.result is None:
            return False, f"{field} change to '{value}' was not confirmed ({outcome.status}).", {}
        current = (outcome.result.instrument if field == "symbol" else outcome.result.timeframe) or ""
        if current.strip().lower() != value.strip().lower():
            return False, f"{field} change to '{value}' was not confirmed (saw {current!r}).", {}
        return (
            True,
            f"TradingView now shows {outcome.result.instrument} on {outcome.result.timeframe}.",
            {"symbol": outcome.result.instrument, "timeframe": outcome.result.timeframe},
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
