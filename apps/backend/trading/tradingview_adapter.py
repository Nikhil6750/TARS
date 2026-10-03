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
        watcher's job, wired independently; see assistant/chart_watch.py).

        Identity priority (mission section 2 -- live testing showed vision
        misreading a tiny "5m" glyph as "6m" while TradingView was
        verifiably, deterministically set to 5m): per field, a symbol or
        timeframe TradingViewControlSkill itself just set and verified
        (`verified_identity()`) always wins over this vision-derived read.
        Vision is used only for whichever field has no verified value yet
        (e.g. the user switched symbol by hand, never through TARS).
        `identity_source` and `vision_identity_conflict` make the
        substitution visible to callers rather than a silent swap."""
        latest = await self._store.get_latest()
        verified = self._tv.verified_identity()

        vision_symbol = latest.identity.symbol if latest else None
        vision_timeframe = latest.identity.timeframe if latest else None

        symbol = verified["symbol"] or vision_symbol
        timeframe = verified["timeframe"] or vision_timeframe
        conflict = bool(
            (verified["symbol"] and vision_symbol and verified["symbol"].upper() != vision_symbol.upper())
            or (verified["timeframe"] and vision_timeframe and verified["timeframe"] != vision_timeframe)
        )
        identity_source = "verified" if (verified["symbol"] or verified["timeframe"]) else "vision"

        if latest is None:
            return {
                "monitoring": False, "state": "NOT FOUND", "detail": "No chart window observed yet",
                "symbol": symbol, "timeframe": timeframe,
                "identity_source": identity_source, "vision_identity_conflict": conflict,
            }
        freshness = latest.freshness().value
        age_s = round(latest.age_ms() / 1000)
        label = f"{symbol or 'chart'} {timeframe or ''}".strip()
        monitoring = freshness in ("hot", "warm")
        return {
            "monitoring": monitoring, "state": "MONITORING" if monitoring else "NOT FOUND",
            "detail": f"{label}, analysed {age_s}s ago", "freshness": freshness,
            "symbol": symbol, "timeframe": timeframe,
            "identity_source": identity_source, "vision_identity_conflict": conflict,
        }
