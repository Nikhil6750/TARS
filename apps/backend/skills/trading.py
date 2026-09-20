"""`trading` skill — the Trading Intelligence foundation's skill surface:
`analyze_active_chart`, `capture_chart`, `get_trading_context`,
`explain_setup`, `search_trading_memory`, `save_trading_observation`,
`open_tradingview`, `focus_tradingview`.

Composes existing building blocks rather than reimplementing them:
- `capture_chart`/`analyze_active_chart` reuse the same
  ActionRuntime -> FrontendCommandBridge capture RPC that
  `windows_app.capture_active_window` and
  `POST /api/v1/assistant/analyze-chart` already use -- no second capture
  path.
- `get_trading_context`/`explain_setup` read `EventService`'s deterministic
  active-setup state through `trading.context.TradingContextBuilder`, and
  the configured `StrategyProvider` -- never invent a strategy read.
  `strategy_status` is always surfaced verbatim (NOT_CONFIGURED by default;
  see trading/provider.py) rather than silently omitted.
- `search_trading_memory`/`save_trading_observation` delegate to
  `MemoryService`, which is also what indexes/searches conversation memory
  and the Obsidian vault -- one memory system, not a second one.

Any dependency that is not wired (no MemoryService, no ChartAnalysisService,
no connected native shell) fails closed at execute() with a clear error,
matching every other skill's contract -- never a fabricated result.
"""
from __future__ import annotations

import base64
import binascii
import webbrowser
from datetime import UTC, datetime
from typing import Any

from actions.frontend_bridge import FrontendBridgeError, FrontendCommandBridge
from app.action_contracts import (
    ActionRequest,
    ActionResult,
    ActionStatus,
    BaseSkill,
    RiskLevel,
    SkillExecutionError,
    SkillValidationError,
)
from assistant.chart_analysis import ChartAnalysisError, ChartAnalysisService
from memory.service import KIND_TRADING_OBSERVATION, MemoryService
from trading.context import TradingContextBuilder

_READ_ONLY_ACTIONS = {
    "capture_chart",
    "analyze_active_chart",
    "get_trading_context",
    "explain_setup",
    "search_trading_memory",
}
_LOW_RISK_ACTIONS = {"save_trading_observation", "open_tradingview", "focus_tradingview"}
_MAX_IMAGE_BYTES = 15 * 1_048_576
_DEFAULT_BRIDGE_TIMEOUT = 15.0
_TRADINGVIEW_URL = "https://www.tradingview.com/chart/"


class TradingSkill(BaseSkill):
    name = "trading"
    description = (
        "Trading Intelligence foundation: chart capture/analysis, deterministic "
        "trading context, trading memory, and TradingView open/focus."
    )
    capabilities: tuple[str, ...] = (
        "analyze_active_chart",
        "capture_chart",
        "get_trading_context",
        "explain_setup",
        "search_trading_memory",
        "save_trading_observation",
        "open_tradingview",
        "focus_tradingview",
    )

    def __init__(
        self,
        *,
        memory_service: MemoryService | None = None,
        chart_analysis_service: ChartAnalysisService | None = None,
        context_builder: TradingContextBuilder | None = None,
        bridge: FrontendCommandBridge | None = None,
    ) -> None:
        self._memory = memory_service
        self._chart_analysis = chart_analysis_service
        self._context_builder = context_builder
        self._bridge = bridge

    async def health(self) -> dict[str, Any]:
        return {
            "available": True,
            "memory_available": self._memory is not None,
            "chart_analysis_available": self._chart_analysis is not None,
            "trading_context_available": self._context_builder is not None,
            "capture_available": self._bridge is not None,
        }

    def classify_risk(self, action: str, arguments: dict[str, Any]) -> RiskLevel:
        if action in _READ_ONLY_ACTIONS:
            return RiskLevel.READ_ONLY
        if action in _LOW_RISK_ACTIONS:
            return RiskLevel.LOW_RISK
        return RiskLevel.BLOCKED

    async def validate(self, action: str, arguments: dict[str, Any]) -> None:
        if action in ("capture_chart", "get_trading_context", "open_tradingview", "focus_tradingview"):
            return
        if action == "analyze_active_chart":
            goal = arguments.get("goal")
            if goal is not None and not isinstance(goal, str):
                raise SkillValidationError("'goal' must be a string if provided")
        elif action == "explain_setup":
            symbol = arguments.get("symbol")
            if not isinstance(symbol, str) or not symbol.strip():
                raise SkillValidationError("explain_setup requires non-empty 'symbol'")
        elif action == "search_trading_memory":
            query = arguments.get("query")
            if not isinstance(query, str) or not query.strip():
                raise SkillValidationError("search_trading_memory requires non-empty 'query'")
        elif action == "save_trading_observation":
            text = arguments.get("text")
            if not isinstance(text, str) or not text.strip():
                raise SkillValidationError("save_trading_observation requires non-empty 'text'")
            tags = arguments.get("tags")
            if tags is not None and (
                not isinstance(tags, list) or not all(isinstance(t, str) for t in tags)
            ):
                raise SkillValidationError("'tags' must be a list of strings if provided")
        else:
            raise SkillValidationError(f"unsupported trading action '{action}'")

    async def execute(self, request: ActionRequest) -> ActionResult:
        started = datetime.now(UTC)
        handler = {
            "capture_chart": self._execute_capture_chart,
            "analyze_active_chart": self._execute_analyze_active_chart,
            "get_trading_context": self._execute_get_trading_context,
            "explain_setup": self._execute_explain_setup,
            "search_trading_memory": self._execute_search_trading_memory,
            "save_trading_observation": self._execute_save_trading_observation,
            "open_tradingview": self._execute_open_tradingview,
            "focus_tradingview": self._execute_focus_tradingview,
        }.get(request.action)
        if handler is None:
            raise SkillExecutionError(f"unsupported trading action '{request.action}'")
        return await handler(request, started)

    # ---- capture / chart analysis --------------------------------------

    async def _dispatch_capture(self, request: ActionRequest) -> dict[str, Any]:
        if self._bridge is None:
            raise SkillExecutionError(
                "trading.capture_chart() requires a connected native shell and no "
                "FrontendCommandBridge is wired -- refusing rather than fabricating a result"
            )
        try:
            return await self._bridge.dispatch(
                request.id,
                "windows_app",
                "capture_active_window",
                {},
                timeout=_DEFAULT_BRIDGE_TIMEOUT,
            )
        except FrontendBridgeError as exc:
            raise SkillExecutionError(str(exc)) from exc

    async def _execute_capture_chart(self, request: ActionRequest, started: datetime) -> ActionResult:
        data = await self._dispatch_capture(request)
        if data.get("is_secure_desktop"):
            return self._result(
                request,
                ActionStatus.FAILED,
                "Capture refused: secure desktop or credential screen active.",
                risk_level=RiskLevel.READ_ONLY,
                data=data,
                error=data.get("error") or "secure desktop capture refused",
                started_at=started,
            )
        return self._result(
            request,
            ActionStatus.SUCCEEDED,
            "Captured the active window for chart analysis.",
            risk_level=RiskLevel.READ_ONLY,
            data=data,
            started_at=started,
        )

    async def _execute_analyze_active_chart(
        self, request: ActionRequest, started: datetime
    ) -> ActionResult:
        if self._chart_analysis is None:
            raise SkillExecutionError(
                "trading.analyze_active_chart() requires a configured ChartAnalysisService"
            )
        data = await self._dispatch_capture(request)
        if data.get("error") or data.get("is_secure_desktop"):
            return self._result(
                request,
                ActionStatus.FAILED,
                "Capture was refused or errored; nothing to analyze.",
                risk_level=RiskLevel.READ_ONLY,
                data=data,
                error=data.get("error") or "secure desktop capture refused",
                started_at=started,
            )
        image_bytes, image_format = _decode_capture_image(data)
        active_context = data.get("active_context")
        active_context_text = (
            _describe_active_context(active_context) if isinstance(active_context, dict) else ""
        )
        goal_text = request.arguments.get("goal") or "Analyze this chart."
        try:
            result = await self._chart_analysis.analyze(
                image_bytes=image_bytes,
                image_format=image_format,
                conversation_id=str(request.id),
                active_context_text=active_context_text,
                goal_text=goal_text,
            )
        except ChartAnalysisError as exc:
            return self._result(
                request,
                ActionStatus.FAILED,
                "Captured image could not be analyzed.",
                risk_level=RiskLevel.READ_ONLY,
                error=str(exc),
                started_at=started,
            )
        return self._result(
            request,
            ActionStatus.SUCCEEDED,
            result.speech_text() or "Chart analyzed.",
            risk_level=RiskLevel.READ_ONLY,
            data=result.to_dict(),
            started_at=started,
        )

    # ---- deterministic trading context / explanation --------------------

    async def _execute_get_trading_context(
        self, request: ActionRequest, started: datetime
    ) -> ActionResult:
        if self._context_builder is None:
            raise SkillExecutionError("trading.get_trading_context() requires a TradingContextBuilder")
        context = await self._context_builder.build()
        return self._result(
            request,
            ActionStatus.SUCCEEDED,
            context.as_grounding_text(),
            risk_level=RiskLevel.READ_ONLY,
            data={
                "strategy_status": context.strategy_status.value,
                "active_setups": context.active_setups,
                "recent_warnings": context.recent_warnings,
                "strategy": context.strategy.__dict__ if context.strategy else None,
            },
            started_at=started,
        )

    async def _execute_explain_setup(self, request: ActionRequest, started: datetime) -> ActionResult:
        if self._context_builder is None:
            raise SkillExecutionError("trading.explain_setup() requires a TradingContextBuilder")
        symbol = request.arguments["symbol"].strip().upper()
        context = await self._context_builder.build()
        match = next((s for s in context.active_setups if s.get("symbol") == symbol), None)
        if match is None:
            return self._result(
                request,
                ActionStatus.SUCCEEDED,
                f"No active setup is currently tracked for {symbol}.",
                risk_level=RiskLevel.READ_ONLY,
                data={"symbol": symbol, "found": False, "strategy_status": context.strategy_status.value},
                started_at=started,
            )
        summary = _explain_setup_deterministic(match, context.strategy_status.value)
        return self._result(
            request,
            ActionStatus.SUCCEEDED,
            summary,
            risk_level=RiskLevel.READ_ONLY,
            data={
                "symbol": symbol,
                "found": True,
                "setup": match,
                "strategy_status": context.strategy_status.value,
            },
            started_at=started,
        )

    # ---- memory -----------------------------------------------------------

    async def _execute_search_trading_memory(
        self, request: ActionRequest, started: datetime
    ) -> ActionResult:
        if self._memory is None:
            raise SkillExecutionError("trading.search_trading_memory() requires a MemoryService")
        query = request.arguments["query"]
        limit = request.arguments.get("limit", 10)
        if not isinstance(limit, int) or isinstance(limit, bool):
            limit = 10
        results = await self._memory.search(query, limit=limit, source=KIND_TRADING_OBSERVATION)
        return self._result(
            request,
            ActionStatus.SUCCEEDED,
            f"Found {len(results)} trading observation(s) for '{query}'.",
            risk_level=RiskLevel.READ_ONLY,
            data={"results": results},
            started_at=started,
        )

    async def _execute_save_trading_observation(
        self, request: ActionRequest, started: datetime
    ) -> ActionResult:
        if self._memory is None:
            raise SkillExecutionError("trading.save_trading_observation() requires a MemoryService")
        text = request.arguments["text"]
        symbol = request.arguments.get("symbol")
        tags = request.arguments.get("tags")
        note_id = await self._memory.save_trading_observation(
            text,
            symbol=symbol.strip().upper() if isinstance(symbol, str) and symbol.strip() else None,
            actor="user",
            tags=tags,
        )
        return self._result(
            request,
            ActionStatus.SUCCEEDED,
            "Saved trading observation.",
            risk_level=RiskLevel.LOW_RISK,
            data={"note_id": note_id},
            started_at=started,
        )

    # ---- TradingView open/focus ------------------------------------------

    async def _execute_open_tradingview(self, request: ActionRequest, started: datetime) -> ActionResult:
        opened = webbrowser.open(_TRADINGVIEW_URL)
        if not opened:
            raise SkillExecutionError("failed to launch a browser for TradingView")
        return self._result(
            request,
            ActionStatus.SUCCEEDED,
            "Opened TradingView in the default browser.",
            risk_level=RiskLevel.LOW_RISK,
            data={"url": _TRADINGVIEW_URL},
            started_at=started,
        )

    async def _execute_focus_tradingview(self, request: ActionRequest, started: datetime) -> ActionResult:
        match = _find_tradingview_window()
        if match is None:
            return self._result(
                request,
                ActionStatus.FAILED,
                "No running TradingView window was found to focus.",
                risk_level=RiskLevel.LOW_RISK,
                error="no visible window matched 'tradingview'",
                started_at=started,
            )
        try:
            _focus_window(match["hwnd"])
        except Exception as exc:
            raise SkillExecutionError(f"failed to focus TradingView window: {exc}") from exc
        return self._result(
            request,
            ActionStatus.SUCCEEDED,
            f"Focused '{match['window_title']}'.",
            risk_level=RiskLevel.LOW_RISK,
            data={"window_title": match["window_title"]},
            started_at=started,
        )


def _decode_capture_image(data: dict[str, Any]) -> tuple[bytes, str]:
    image_data = data.get("image_data_base64")
    if not isinstance(image_data, str) or not image_data.strip():
        raise SkillExecutionError("capture did not include image_data_base64")
    image_format = str(data.get("image_format") or "image/png")
    _, _, encoded = image_data.partition(",") if image_data.startswith("data:") else ("", "", image_data)
    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SkillExecutionError(f"captured image was not valid base64: {exc}") from exc
    if len(image_bytes) > _MAX_IMAGE_BYTES:
        raise SkillExecutionError("captured image exceeds the size limit")
    return image_bytes, image_format


def _describe_active_context(active_context: dict[str, Any]) -> str:
    parts = []
    executable = active_context.get("executable")
    title = active_context.get("window_title")
    if executable:
        parts.append(f"application: {executable}")
    if title:
        parts.append(f"window title: {title}")
    return "; ".join(parts)


def _explain_setup_deterministic(setup: dict[str, Any], strategy_status: str) -> str:
    parts = [f"{setup.get('symbol')} is currently {setup.get('state')}"]
    if setup.get("direction"):
        parts.append(f"direction {setup['direction']}")
    if setup.get("entry") is not None:
        parts.append(f"entry {setup['entry']}")
    if setup.get("stop_loss") is not None:
        parts.append(f"stop loss {setup['stop_loss']}")
    if setup.get("take_profit") is not None:
        parts.append(f"take profit {setup['take_profit']}")
    if setup.get("risk_reward") is not None:
        parts.append(f"R:R {setup['risk_reward']}")
    reason_codes = setup.get("reason_codes") or []
    if reason_codes:
        parts.append(f"reason codes: {', '.join(reason_codes)}")
    summary = ", ".join(parts) + "."
    if strategy_status == "NOT_CONFIGURED":
        summary += (
            " No strategy is configured, so this is deterministic setup state only -- "
            "not an evaluation against any strategy's rules."
        )
    return summary


def _find_tradingview_window() -> dict[str, Any] | None:
    try:
        import win32gui
        import win32process
    except ImportError:
        return None

    windows: list[dict[str, Any]] = []

    def _callback(hwnd: int, _extra: None) -> None:
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd)
            if "tradingview" not in title.lower():
                return
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
            except Exception:
                pid = None
            windows.append({"hwnd": hwnd, "window_title": title, "process_id": pid})
        except Exception:
            return

    try:
        win32gui.EnumWindows(_callback, None)
    except Exception:
        pass
    return windows[0] if windows else None


def _focus_window(hwnd: int) -> None:
    import win32con
    import win32gui

    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            win32gui.BringWindowToTop(hwnd)
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    except Exception:
        pass
