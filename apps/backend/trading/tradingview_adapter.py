"""TradingViewAdapter -- the one contract other code (MarketContext builder,
voice tools, a future caller) uses to read/control TradingView.

Mission: "Create/finish ONE TradingViewAdapter contract... Do not create
another parallel TradingView framework." Every method here delegates to
EXISTING, already-tested machinery rather than touching Windows UI/vision
directly:

  * status/focus/set_symbol/set_timeframe -> `skills.tradingview_control.
    TradingViewControlSkill`'s own `execute()`, given a constructed
    `ActionRequest` -- the exact same shape `ActionRuntime` itself builds,
    so this calls the real keystroke-simulation + title/vision verification
    path, never a second implementation of it.
  * capture_chart/analyze_chart -> `skills.trading.TradingSkill`'s
    `capture_chart`/`analyze_active_chart`, same calling convention.
  * current_symbol/current_timeframe/monitor_chart -> read
    `HotChartStateStore` directly, the same read
    `MonitorManager.tradingview_status()` already does -- read-only, never
    starts or replaces the native BackgroundChartWatcher.
"""
from __future__ import annotations

from typing import Any

from app.action_contracts import ActionRequest, ActionSource, SkillExecutionError
from skills._desktop_automation import resolve_window


class TradingViewAdapter:
    def __init__(self, tradingview_skill, trading_skill, hot_chart_store, *,
                 source: ActionSource = ActionSource.deterministic) -> None:
        self._tv = tradingview_skill
        self._trading = trading_skill
        self._store = hot_chart_store
        self._source = source

    async def _call(self, skill, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        request = ActionRequest(skill=skill.name, action=action, arguments=arguments, source=self._source)
        result = await skill.execute(request)
        return {"status": result.status.value, "summary": result.summary,
                "data": result.data, "error": result.error}

    # ---- control (delegates to TradingViewControlSkill) --------------------
    async def status(self) -> dict[str, Any]:
        return await self._call(self._tv, "status", {})

    async def focus(self) -> dict[str, Any]:
        return await self._call(self._tv, "focus", {})

    async def set_symbol(self, symbol: str) -> dict[str, Any]:
        return await self._call(self._tv, "set_symbol", {"symbol": symbol})

    async def set_timeframe(self, timeframe: str) -> dict[str, Any]:
        return await self._call(self._tv, "set_timeframe", {"timeframe": timeframe})

    # ---- chart capture/analysis (delegates to TradingSkill) ----------------
    async def capture_chart(self) -> dict[str, Any]:
        return await self._call(self._trading, "capture_chart", {})

    async def analyze_chart(self, question: str = "Analyze this chart.") -> dict[str, Any]:
        return await self._call(self._trading, "analyze_active_chart", {"question": question})

    # ---- read-only state (reads HotChartStateStore directly) ---------------
    @staticmethod
    def _window() -> tuple[int, str, str] | None:
        try:
            return resolve_window("tradingview")
        except SkillExecutionError:
            return None

    async def current_symbol(self) -> str | None:
        found = self._window()
        if found is None:
            return None
        hwnd, _exe, _title = found
        state = await self._store.get_latest_for_window(str(hwnd))
        return state.identity.symbol if state else None

    async def current_timeframe(self) -> str | None:
        found = self._window()
        if found is None:
            return None
        hwnd, _exe, _title = found
        state = await self._store.get_latest_for_window(str(hwnd))
        return state.identity.timeframe if state else None

    async def monitor_chart(self) -> dict[str, Any]:
        """Whether the native background watcher currently has a fresh read
        of a TradingView chart -- never starts one (that is the Rust-side
        watcher's job, wired independently; see assistant/chart_watch.py)."""
        latest = await self._store.get_latest()
        if latest is None:
            return {"monitoring": False, "state": "NOT FOUND", "detail": "No chart window observed yet",
                    "symbol": None, "timeframe": None}
        freshness = latest.freshness().value
        age_s = round(latest.age_ms() / 1000)
        label = f"{latest.identity.symbol or 'chart'} {latest.identity.timeframe or ''}".strip()
        monitoring = freshness in ("hot", "warm")
        return {"monitoring": monitoring, "state": "MONITORING" if monitoring else "NOT FOUND",
                "detail": f"{label}, analysed {age_s}s ago", "freshness": freshness,
                "symbol": latest.identity.symbol, "timeframe": latest.identity.timeframe}
