"""Bounded desktop tools for the voice agent. NOT a second control framework.

Every tool builds an ActionRequest and submits it to the existing ActionRuntime, which applies the
PermissionEngine (READ_ONLY / LOW_RISK run, CONFIRM_REQUIRED waits, BLOCKED never runs) and audits
each request. This module only adds:
  * mapping of runtime outcomes to truthful states: DONE / NOT_FOUND / NEEDS_CONFIRMATION / BLOCKED / FAILED
  * a voice confirmation gate: the confirmation token never reaches the model; a pending action runs
    only if the HUMAN's own latest final transcript (heard after the request) is an explicit "yes"
  * a live-trading guard: no click/type/terminal path may reach order placement (MT5 stays read-only)
  * lightweight desktop context (active window, recent actions), fetched on demand only.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from collections.abc import Callable
from typing import Any
from uuid import UUID

from actions.errors import ConfirmationReplayError
from app.action_contracts import ActionRequest, ActionSource, ActionStatus, RiskLevel
from skills.app_resolver import ALIASES, _normalize

logger = logging.getLogger("tars.desktop_tools")

DESKTOP_TOOL_NAMES = {
    "desktop_context", "desktop_resolve_app", "desktop_list_installed_apps", "desktop_open_app",
    "desktop_focus_window", "desktop_close_app", "desktop_list_controls", "desktop_click_control",
    "desktop_type_text", "desktop_scroll", "calculator_calculate", "browser_open_url", "browser_search", "files_list",
    "files_read_open", "run_terminal", "analyze_chart", "watch_this_chart", "confirm_pending_action",
    "cancel_pending_action",
    "tradingview_status", "tradingview_set_symbol", "tradingview_set_timeframe",
    "web_get_context", "web_list_tabs", "web_focus_tab", "web_new_tab", "web_close_tab",
    "web_navigate", "web_back", "web_forward", "web_refresh",
    "web_find", "web_click", "web_type", "web_select", "web_scroll", "web_wait_for",
    "web_extract_text", "web_extract_table", "web_get_links",
    "web_download", "web_get_last_download",
}

# Fast-path tools: a direct deterministic action with no reasoning/planning
# call involved (mission item 11/26 "fast path") -- every desktop_/web_/
# tradingview_/browser_ tool above already qualifies, since each one maps
# straight onto one ActionRequest; this set exists only to tag the
# diagnostics log line (item 17/26), not to change dispatch.
FAST_PATH_TOOLS = DESKTOP_TOOL_NAMES - {"analyze_chart", "confirm_pending_action", "cancel_pending_action"}

# Which resolved app aliases count as "the current trading app" for context tracking (item 1 of
# the trading-desktop mission: current_trading_app must survive normal voice turns so "switch it
# to EURUSD" after "open TradingView" doesn't need the app named again).
_TRADING_APPS = {"tradingview": "TradingView", "metatrader 5": "MetaTrader 5"}

_TRADE_WORDS = re.compile(
    r"\b(buy|sell|order|new order|place|modify|close|close all|delete|cancel|trade|deal|market execution|"
    r"one.click|reverse|hedge|f9)\b", re.IGNORECASE)
_MT5_WINDOW = re.compile(r"metatrader|metaquotes|terminal64|mt5", re.IGNORECASE)
_TRADE_TERMINAL = re.compile(r"order_send|ordersend|order_check|mt5\.(buy|sell)|positions_close|trade_request",
                             re.IGNORECASE)
_AFFIRMATIVE = re.compile(r"\b(yes|yeah|yep|confirm|confirmed|go ahead|do it|proceed|sure|okay|ok)\b", re.IGNORECASE)
_NEGATIVE = re.compile(r"\b(no|nope|don't|do not|cancel|stop|never mind|abort)\b", re.IGNORECASE)


def _state(result) -> str:
    status = result.status
    if status is ActionStatus.SUCCEEDED:
        return "DONE"
    if status is ActionStatus.CONFIRMATION_REQUIRED:
        return "NEEDS_CONFIRMATION"
    if status in (ActionStatus.DENIED, ActionStatus.BLOCKED):
        return "BLOCKED"
    outcome = str((result.data or {}).get("outcome") or "")
    text = f"{result.error or ''} {result.summary} {outcome}".lower()
    if status is ActionStatus.FAILED:
        if re.search(r"\bambiguous\b", text):
            return "AMBIGUOUS"
        if re.search(r"not found|no such|no running|not_found|could not find|no match", text):
            return "NOT_FOUND"
        if re.search(r"\bpartial\b", text):
            return "PARTIAL"
    return "FAILED"


def _trim(data: Any, limit: int = 3500) -> Any:
    text = json.dumps(data, default=str)
    if len(text) <= limit:
        return data
    return {"truncated": True, "preview": text[:limit]}


class DesktopTools:
    def __init__(self, app_state, last_user: Callable[[], tuple[str, float]], *, source=ActionSource.voice_wake_word):
        self.state = app_state
        self.last_user = last_user  # -> (latest final user transcript, monotonic time heard)
        self.source = source
        self.pending: dict | None = None
        # Mission: P0 regression -- "never guess if more than one
        # confirmation is active." `self.pending` alone (the single-slot
        # "most recent") silently overwrote an earlier unresolved
        # confirmation if a second one started before the first was
        # answered. This additional list tracks every confirmation this
        # session has asked for that hasn't been resolved yet, purely so
        # confirm/cancel can detect "more than one" and refuse to guess --
        # it never changes the single-pending fast path's existing
        # behavior or shape.
        self._pending_queue: list[dict] = []
        self.recent: deque[dict] = deque(maxlen=10)
        # Mission section 21's learning hook: an in-memory-only record of
        # each successful tool call, in the shape a future Skill Learning
        # system would consume ({trigger, intent, parameters, steps,
        # verification, duration, provider}). Deliberately NOT a database
        # table or a file -- the mission is explicit that nothing should be
        # persisted yet -- just a seam with the right shape for when that
        # work happens. Bounded so it cannot grow across a long session.
        self.execution_traces: deque[dict] = deque(maxlen=50)
        self._ctx_cache: tuple[float, dict] | None = None
        # Session-scoped desktop/trading context (item 1: survives normal voice turns; Gemini's
        # own conversation memory handles pronoun resolution across turns, this is what backs a
        # truthful answer when asked outright, and what a same-app follow-up tool call implicitly
        # targets without needing the app named again).
        self.last_target_app: str | None = None
        self.current_trading_app: str | None = None
        self.current_symbol: str | None = None
        self.current_timeframe: str | None = None
        # Universal Market Explainer follow-up context (mission: "What
        # about 5 minutes?" / "Any news coming?" / "What caused that
        # move?" must inherit the asset without the user repeating it).
        # current_symbol/current_timeframe above already double as
        # provider_symbol/tradingview_symbol -- there is deliberately no
        # second, separately-tracked "current asset" field that could
        # drift out of sync with them.
        self.last_analysis_at: str | None = None
        self.last_chart_observations: list[dict] = []
        self.last_relevant_events: list[dict] = []
        self.last_relevant_news: list[dict] = []
        # Browser agent context (section 4): mirrors the trading-app fields
        # above for the web skill -- so "open the first result" after
        # "search YouTube for..." targets the tab TARS itself just opened,
        # without the caller re-stating it.
        self.active_browser_tab: dict | None = None

    # ---- core submit ------------------------------------------------------
    async def _submit(self, skill: str, action: str, arguments: dict, *, describe: str) -> dict:
        runtime = getattr(self.state, "action_runtime", None)
        if runtime is None:
            return {"status": "FAILED", "summary": "The action runtime is not running"}
        request = ActionRequest(skill=skill, action=action, arguments=arguments, source=self.source)
        action_started = time.monotonic()
        try:
            result = await asyncio.wait_for(runtime.submit(request), 45)
        except TimeoutError:
            outcome = {"status": "FAILED", "summary": f"{describe} timed out"}
            self._remember(describe, outcome["status"])
            self._log_diagnostics(skill, action, arguments, outcome["status"], action_started)
            return outcome
        except Exception as exc:  # validation errors etc. are reported, never hidden
            outcome = {"status": "FAILED", "summary": f"{describe} was rejected: {type(exc).__name__}: {str(exc)[:200]}"}
            self._remember(describe, outcome["status"])
            self._log_diagnostics(skill, action, arguments, outcome["status"], action_started)
            return outcome
        state = _state(result)
        outcome = {"status": state, "summary": result.summary, "risk": result.risk_level.value if result.risk_level else None}
        if state == "NEEDS_CONFIRMATION":
            token = (result.data or {}).get("confirmation_token")
            self.pending = {"id": str(result.request_id), "token": token, "at": time.monotonic(), "describe": describe}
            self._pending_queue.append(self.pending)
            outcome["message"] = (f"This needs the user's explicit confirmation: {describe}. Ask them to say yes or no, "
                                  "then call confirm_pending_action or cancel_pending_action.")
        elif result.data:
            outcome["data"] = _trim({k: v for k, v in result.data.items() if k != "confirmation_token"})
        if result.error and state != "DONE":
            outcome["error"] = result.error[:300]
        self._remember(describe, state)
        self._log_diagnostics(skill, action, arguments, state, action_started)
        return outcome

    def _log_diagnostics(self, skill: str, action: str, arguments: dict, result: str, started: float) -> None:
        # Mission item 17/26: one structured line per action -- skill/action
        # pair (the deterministic "method" Codex actually dispatched, i.e.
        # what the fast-path router chose), target, truthful result, and
        # latency from action-start to verified-result.
        target = arguments.get("target") or arguments.get("url") or arguments.get("control_id") or ""
        logger.info(
            "[diagnostics] skill=%s action=%s target=%r result=%s latency_ms=%.0f",
            skill, action, target, result, (time.monotonic() - started) * 1000,
        )

    def _remember(self, describe: str, state: str):
        self.recent.append({"action": describe, "result": state, "at": time.strftime("%H:%M:%S")})

    async def call(self, name: str, args: dict) -> dict:
        started = time.monotonic()
        result = {}
        try:
            result = await getattr(self, name)(**args)
            return result
        finally:
            latency_ms = (time.monotonic() - started) * 1000
            logger.info("[diagnostics] tool=%s fast_path=%s total_latency_ms=%.0f", name, name in FAST_PATH_TOOLS, latency_ms)
            if isinstance(result, dict) and result.get("status") == "DONE":
                self._record_execution_trace(name, args, result, latency_ms)

    def _record_execution_trace(self, name: str, args: dict, result: dict, latency_ms: float) -> None:
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        self.execution_traces.append({
            "trigger": name, "intent": name, "parameters": args,
            "steps": [{"action": name, "result": "DONE"}],
            "verification": data.get("outcome", "SUCCESS"),
            "duration_ms": round(latency_ms),
            "provider": data.get("provider") or ("web" if name.startswith("web_") else
                                                   "tradingview" if name.startswith("tradingview_") else
                                                   "desktop"),
        })

    # ---- context ------------------------------------------------------------
    async def desktop_context(self) -> dict:
        now = time.monotonic()
        if self._ctx_cache and now - self._ctx_cache[0] < 3:
            screen = self._ctx_cache[1]
        else:
            screen = await self._submit("desktop_control", "inspect_screen", {},
                                        describe="inspect what is visible on screen")
            self._ctx_cache = (now, screen)
        return {
            "primary_visible_window": screen,
            "note": (
                "primary_visible_window is a FRESH, just-now observation of the real primary "
                "window on screen (TARS's own companion/orb/activity-pill window is excluded, "
                "never reported as the answer here) -- it is the ONLY truthful source for "
                "'what is on my screen' / 'what app is open' / 'this' / 'here'. The fields under "
                "previously_opened_by_tars are session memory of apps TARS itself opened or "
                "focused earlier in this conversation and may be stale or no longer visible -- "
                "never use them to answer a screen-visibility question; they only matter for a "
                "same-app follow-up like 'switch it to EURUSD' after 'open TradingView'."
            ),
            "previously_opened_by_tars": {
                "last_target_app": self.last_target_app,
                "current_trading_app": self.current_trading_app,
                "current_symbol": self.current_symbol,
                "current_timeframe": self.current_timeframe,
            },
            "recent_actions": list(self.recent),
            "active_browser_tab": self.active_browser_tab,
        }

    def _note_target_app(self, target: str) -> None:
        self.last_target_app = target
        canonical = ALIASES.get(_normalize(target), _normalize(target))
        trading_name = _TRADING_APPS.get(canonical)
        if trading_name:
            self.current_trading_app = trading_name

    async def _active_is_mt5(self) -> bool:
        ctx = await self.desktop_context()
        return bool(_MT5_WINDOW.search(json.dumps(ctx.get("primary_visible_window", {}), default=str)))

    # ---- read-only: app resolution --------------------------------------------
    async def desktop_resolve_app(self, target: str = "") -> dict:
        return await self._submit("windows_app", "resolve", {"target": target}, describe=f"resolve {target}")

    async def desktop_list_installed_apps(self, query: str = "") -> dict:
        return await self._submit("windows_app", "list_installed", {"query": query},
                                  describe="list installed applications")

    # ---- low-risk actions -----------------------------------------------------
    async def desktop_open_app(self, target: str = "") -> dict:
        out = await self._submit("windows_app", "launch", {"target": target}, describe=f"open {target}")
        if out["status"] == "DONE":
            self._note_target_app(target)
        return out

    async def desktop_focus_window(self, target: str = "") -> dict:
        out = await self._submit("windows_app", "focus", {"target": target}, describe=f"switch to {target}")
        if out["status"] == "DONE":
            self._note_target_app(target)
        return out

    async def desktop_close_app(self, target: str = "") -> dict:
        return await self._submit("windows_app", "close", {"target": target}, describe=f"close {target}")

    async def calculator_calculate(self, expression: str = "") -> dict:
        """"Calculate 2345 times 17" -- opens Calculator (verified
        foreground), performs ONE strictly-validated arithmetic expression
        via Calculator's own real buttons, and verifies the displayed
        result. Never asks for confirmation (mission section 11): the
        calculator skill itself only ever touches the real, sandboxed
        Calculator app with a pre-validated numeric expression -- this is
        not a route to typing into any other app or control."""
        open_out = await self.desktop_open_app("calculator")
        if open_out["status"] not in ("DONE",):
            return open_out
        return await self._submit("calculator", "calculate", {"expression": expression},
                                  describe=f"calculate {expression}")

    # ---- TradingView (item 5/6: read via the existing HotChartState/BackgroundChartWatcher
    # vision pipeline, write via real keystroke simulation -- see skills/tradingview_control.py) --
    async def tradingview_status(self) -> dict:
        out = await self._submit("tradingview", "status", {}, describe="check TradingView")
        self._sync_tradingview(out)
        return out

    async def tradingview_set_symbol(self, symbol: str = "") -> dict:
        out = await self._submit("tradingview", "set_symbol", {"symbol": symbol}, describe=f"switch TradingView to {symbol}")
        if out["status"] == "DONE":
            self.current_trading_app = "TradingView"
        self._sync_tradingview(out)
        return out

    async def tradingview_set_timeframe(self, timeframe: str = "") -> dict:
        out = await self._submit("tradingview", "set_timeframe", {"timeframe": timeframe},
                                 describe=f"set TradingView timeframe to {timeframe}")
        if out["status"] == "DONE":
            self.current_trading_app = "TradingView"
        self._sync_tradingview(out)
        return out

    def _sync_tradingview(self, out: dict) -> None:
        data = out.get("data") or {}
        if data.get("symbol"):
            self.current_symbol = data["symbol"]
        if data.get("timeframe"):
            self.current_timeframe = data["timeframe"]

    # ---- BrowserAgent: real Chrome via CDP (skills/web_browser.py) --------
    def _sync_browser_tab(self, out: dict) -> None:
        data = out.get("data") or {}
        url, title = data.get("url"), data.get("title")
        if url or title:
            self.active_browser_tab = {"url": url, "title": title, "id": data.get("id") or data.get("active_tab_id")}

    async def web_get_context(self) -> dict:
        out = await self._submit("web", "get_context", {}, describe="check the browser")
        self._sync_browser_tab(out)
        return out

    async def web_list_tabs(self) -> dict:
        return await self._submit("web", "list_tabs", {}, describe="list browser tabs")

    async def web_focus_tab(self, target: str = "") -> dict:
        out = await self._submit("web", "focus_tab", {"target": target}, describe=f"switch to tab '{target}'")
        self._sync_browser_tab(out)
        return out

    async def web_new_tab(self, url: str = "") -> dict:
        out = await self._submit("web", "new_tab", {"url": url}, describe=f"open a new tab{f' at {url}' if url else ''}")
        self._sync_browser_tab(out)
        return out

    async def web_close_tab(self, target: str = "") -> dict:
        return await self._submit("web", "close_tab", {"target": target}, describe="close the browser tab")

    async def web_navigate(self, url: str = "") -> dict:
        out = await self._submit("web", "navigate", {"url": url}, describe=f"go to {url}")
        self._sync_browser_tab(out)
        return out

    async def web_back(self) -> dict:
        out = await self._submit("web", "back", {}, describe="go back")
        self._sync_browser_tab(out)
        return out

    async def web_forward(self) -> dict:
        out = await self._submit("web", "forward", {}, describe="go forward")
        self._sync_browser_tab(out)
        return out

    async def web_refresh(self) -> dict:
        out = await self._submit("web", "refresh", {}, describe="refresh the page")
        self._sync_browser_tab(out)
        return out

    async def web_find(self, target: str = "") -> dict:
        return await self._submit("web", "find", {"target": target}, describe=f"find '{target}' on the page")

    async def web_click(self, target: str = "") -> dict:
        out = await self._submit("web", "click", {"target": target}, describe=f"click '{target}'")
        self._sync_browser_tab(out)
        return out

    async def web_type(self, target: str = "", text: str = "", submit: bool = False) -> dict:
        return await self._submit("web", "type", {"target": target, "text": text, "submit": submit},
                                  describe=f"type into '{target}'")

    async def web_select(self, target: str = "", value: str = "") -> dict:
        return await self._submit("web", "select", {"target": target, "value": value},
                                  describe=f"select '{value}' in '{target}'")

    async def web_scroll(self, direction: str = "down", target: str = "") -> dict:
        args = {"direction": direction}
        if target:
            args["target"] = target
        return await self._submit("web", "scroll", args, describe=f"scroll {direction}" if not target else f"scroll to '{target}'")

    async def web_wait_for(self, target: str = "", timeout: float = 10.0) -> dict:
        return await self._submit("web", "wait_for", {"target": target, "timeout": timeout},
                                  describe=f"wait for '{target}'")

    async def web_extract_text(self, mode: str = "summary") -> dict:
        return await self._submit("web", "extract_text", {"mode": mode}, describe="read the page text")

    async def web_extract_table(self, target: str = "") -> dict:
        return await self._submit("web", "extract_table", {"target": target}, describe="extract a table from the page")

    async def web_get_links(self) -> dict:
        return await self._submit("web", "get_links", {}, describe="list links on the page")

    async def web_download(self, target: str = "") -> dict:
        return await self._submit("web", "download", {"target": target}, describe=f"download '{target}'")

    async def web_get_last_download(self) -> dict:
        return await self._submit("web", "get_last_download", {}, describe="check the last download")

    async def desktop_list_controls(self, target: str = "") -> dict:
        args = {"max_controls": 60, "max_depth": 5}
        if target:
            args["target"] = target
        return await self._submit("desktop_control", "list_controls", args, describe="list on-screen controls")

    async def desktop_scroll(self, control_id: str = "", direction: str = "down") -> dict:
        return await self._submit("desktop_control", "scroll_control",
                                  {"control_id": control_id, "direction": direction, "amount": "small"},
                                  describe=f"scroll {direction}")

    async def browser_open_url(self, url: str = "") -> dict:
        return await self._submit("browser", "open_url", {"url": url}, describe=f"open {url}")

    async def browser_search(self, query: str = "") -> dict:
        return await self._submit("browser", "search", {"query": query}, describe=f"search the web for {query}")

    async def files_list(self, path: str = "", query: str = "") -> dict:
        if query:
            return await self._submit("filesystem", "search", {"path": path or ".", "query": query},
                                      describe=f"search files for {query}")
        return await self._submit("filesystem", "list", {"path": path or "."}, describe=f"list {path or 'home folder'}")

    async def files_read_open(self, path: str = "") -> dict:
        return await self._submit("filesystem", "open", {"path": path}, describe=f"open {path}")

    # ---- state-changing actions: runtime demands confirmation; MT5 orders are blocked ----
    async def _guard_trading(self, label: str, control_id: str, text: str = "") -> dict | None:
        blob = f"{label} {control_id} {text}"
        if _TRADE_WORDS.search(blob) and await self._active_is_mt5():
            outcome = {"status": "BLOCKED", "summary": "TARS is read-only for live trading: it will not click or type "
                       "order, buy, sell, modify or close controls in MetaTrader. Use MT5 yourself for that."}
            self._remember(f"click {label or control_id} in MT5", "BLOCKED")
            return outcome
        return None

    async def desktop_click_control(self, control_id: str = "", label: str = "") -> dict:
        blocked = await self._guard_trading(label, control_id)
        if blocked:
            return blocked
        return await self._submit("desktop_control", "invoke_control", {"control_id": control_id},
                                  describe=f"click '{label or control_id}'")

    async def desktop_type_text(self, control_id: str = "", text: str = "", label: str = "") -> dict:
        blocked = await self._guard_trading(label, control_id, text)
        if blocked:
            return blocked
        return await self._submit("desktop_control", "type_into_control",
                                  {"control_id": control_id, "text": text, "mode": "replace"},
                                  describe=f"type into '{label or control_id}'")

    async def run_terminal(self, command: str = "") -> dict:
        if _TRADE_TERMINAL.search(command or ""):
            return {"status": "BLOCKED", "summary": "Order placement through scripts is not allowed; trading is read-only."}
        return await self._submit("terminal", "run_command", {"command": command}, describe=f"run '{command[:60]}'")

    async def analyze_chart(self, question: str = "") -> dict:
        """Synchronous, bounded "analyze this chart" path -- goes through
        AssistantTurnController._chart_analysis(), which captures the
        primary visible window FRESH on every call and analyzes it in this
        same turn. Never the background watcher: see watch_this_chart()
        for the separate, explicit "watch/monitor" command, which this
        method must never be used to implement."""
        turns = getattr(self.state, "turn_controller", None)
        if turns is None:
            return {"status": "FAILED", "summary": "The assistant backend is not running"}
        answer = ""
        try:
            # Capture (~20s bound) + ChartAnalysisService's own provider
            # budget (settings.chart_analysis_timeout_seconds, 120s by
            # default) + margin -- must not cut off a real, in-flight
            # vision call against a large chart screenshot.
            async with asyncio.timeout(150):
                async for event in turns.stream_text(f"analyze the chart. {question}".strip(), conversation_id="voice-chart",
                                                     turn_id=None, speak=False):
                    if event.type == "complete" and event.response:
                        if event.response.status.value == "failed":
                            return {"status": "FAILED", "summary": "Chart analysis failed"}
                        answer = event.response.display_text
        except TimeoutError:
            return {"status": "FAILED", "summary": "Chart analysis timed out"}
        self._remember("analyze the chart", "DONE")
        return {"status": "DONE", "analysis": answer[:2500]}

    async def watch_this_chart(self) -> dict:
        """Explicit "watch this for me" / "monitor EURUSD" command. TARS's
        background chart watcher already runs continuously and tracks any
        supported chart window on its own -- this gives the user a
        truthful, IMMEDIATE acknowledgement that it is doing so (or an
        honest NOT_A_CHART if there is nothing to watch) instead of silently
        doing nothing, or instead of running a full vision analysis. This is
        the only "watch" entry point; analyze_chart() must never be used for
        a watch/monitor request, and this must never be used to answer an
        "analyze"/"what do you see" request."""
        out = await self._submit("tradingview", "status", {}, describe="check whether a supported chart is visible to watch")
        self._sync_tradingview(out)
        if out.get("status") != "DONE":
            return {"status": "NOT_A_CHART", "summary": "I don't see a supported chart window open to watch right now."}
        self._remember("watch this chart", "DONE")
        return {"status": "DONE", "summary": "Watching this chart in the background -- I'll keep tracking it as it updates."}

    # ---- human confirmation gate ------------------------------------------------
    def _discard_from_queue(self, request_id: str) -> None:
        self._pending_queue = [p for p in self._pending_queue if p["id"] != request_id]

    def _ambiguous_pending_response(self) -> dict:
        """Mission: P0 regression section 8 -- never guess which pending
        confirmation a bare "yes" refers to when more than one is
        outstanding. Each must be resolved through its own confirmation_id
        (the UI card is tied to one specific request already)."""
        names = ", ".join(p["describe"] for p in self._pending_queue)
        return {
            "status": "AMBIGUOUS",
            "summary": f"There are {len(self._pending_queue)} actions waiting for confirmation ({names}). "
                      "I can't tell which one 'yes' is for -- please confirm or deny each one from its own prompt.",
        }

    async def confirm_pending_action(self) -> dict:
        pending = self.pending
        if not pending:
            return {"status": "NOT_FOUND", "summary": "There is no action waiting for confirmation."}
        if len(self._pending_queue) > 1:
            return self._ambiguous_pending_response()
        # Mission: P0 regression -- Gemini can call this tool as part of
        # the SAME turn that transcribed the user's "yes," before
        # _finalize_user() has run (that only happens once the assistant's
        # own reply starts, which can be AFTER the tool call). last_user()
        # now resolves to whichever is more recent -- the finalized
        # transcript, or the still-in-progress one -- so a "yes" already
        # captured in the live transcript is not mistaken for "not
        # answered yet" just because finalization hasn't caught up.
        heard, heard_at = self.last_user()
        if time.monotonic() - pending["at"] > 110 or heard_at < pending["at"]:
            return {"status": "NEEDS_CONFIRMATION", "summary": "The user has not answered yet. Ask them to say yes or no."}
        if _NEGATIVE.search(heard) or not _AFFIRMATIVE.search(heard):
            return {"status": "NEEDS_CONFIRMATION",
                    "summary": f"The user's last words were not an explicit yes ('{heard[:60]}'). Do not run it."}
        runtime = self.state.action_runtime
        self.pending = None
        self._discard_from_queue(pending["id"])
        try:
            result = await asyncio.wait_for(runtime.confirm(UUID(pending["id"]), pending["token"], True), 45)
        except ConfirmationReplayError:
            # Mission: P0 regression -- the UI (or a duplicate voice call)
            # already resolved this exact confirmation; that is not a
            # failure, it is "already handled," and must be reported as
            # such rather than a confusing FAILED.
            return {"status": "ALREADY_HANDLED", "summary": "That was already confirmed or denied -- nothing more to do."}
        except Exception as exc:
            self._remember(pending["describe"], "FAILED")
            return {"status": "FAILED", "summary": f"Confirmation failed: {type(exc).__name__}"}
        state = _state(result)
        self._remember(pending["describe"], state)
        return {"status": state, "summary": result.summary, "data": _trim(result.data or {})}

    async def ui_confirm(self, approve: bool) -> dict:
        """Human clicked Yes/No in the app UI: same ActionRuntime confirm call, no transcript needed."""
        if not approve:
            return await self.cancel_pending_action()
        pending = self.pending
        if not pending:
            return {"status": "NOT_FOUND", "summary": "Nothing was waiting for confirmation."}
        if len(self._pending_queue) > 1:
            return self._ambiguous_pending_response()
        self.pending = None
        self._discard_from_queue(pending["id"])
        try:
            result = await asyncio.wait_for(self.state.action_runtime.confirm(UUID(pending["id"]), pending["token"], True), 45)
        except ConfirmationReplayError:
            return {"status": "ALREADY_HANDLED", "summary": "That was already confirmed or denied -- nothing more to do."}
        except Exception as exc:
            self._remember(pending["describe"], "FAILED")
            return {"status": "FAILED", "summary": f"Confirmation failed: {type(exc).__name__}"}
        state = _state(result)
        self._remember(pending["describe"], state)
        return {"status": state, "summary": result.summary}

    async def cancel_pending_action(self) -> dict:
        pending, self.pending = self.pending, None
        if not pending:
            return {"status": "NOT_FOUND", "summary": "Nothing was waiting for confirmation."}
        self._discard_from_queue(pending["id"])
        try:
            await self.state.action_runtime.confirm(UUID(pending["id"]), pending["token"], False)
        except Exception:
            pass
        self._remember(pending["describe"], "BLOCKED")
        return {"status": "DONE", "summary": "Cancelled; the action did not run."}
