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
import re
import time
from collections import deque
from collections.abc import Callable
from typing import Any
from uuid import UUID

from app.action_contracts import ActionRequest, ActionSource, ActionStatus, RiskLevel

DESKTOP_TOOL_NAMES = {
    "desktop_context", "desktop_open_app", "desktop_focus_window", "desktop_list_controls",
    "desktop_click_control", "desktop_type_text", "desktop_scroll", "browser_open_url", "browser_search",
    "files_list", "files_read_open", "run_terminal", "analyze_chart", "confirm_pending_action",
    "cancel_pending_action",
}

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
    text = f"{result.error or ''} {result.summary}".lower()
    if status is ActionStatus.FAILED and re.search(r"not found|no such|no running|not_found|could not find|no match", text):
        return "NOT_FOUND"
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
        self.recent: deque[dict] = deque(maxlen=10)
        self._ctx_cache: tuple[float, dict] | None = None

    # ---- core submit ------------------------------------------------------
    async def _submit(self, skill: str, action: str, arguments: dict, *, describe: str) -> dict:
        runtime = getattr(self.state, "action_runtime", None)
        if runtime is None:
            return {"status": "FAILED", "summary": "The action runtime is not running"}
        request = ActionRequest(skill=skill, action=action, arguments=arguments, source=self.source)
        try:
            result = await asyncio.wait_for(runtime.submit(request), 45)
        except TimeoutError:
            outcome = {"status": "FAILED", "summary": f"{describe} timed out"}
            self._remember(describe, outcome["status"])
            return outcome
        except Exception as exc:  # validation errors etc. are reported, never hidden
            outcome = {"status": "FAILED", "summary": f"{describe} was rejected: {type(exc).__name__}: {str(exc)[:200]}"}
            self._remember(describe, outcome["status"])
            return outcome
        state = _state(result)
        outcome = {"status": state, "summary": result.summary, "risk": result.risk_level.value if result.risk_level else None}
        if state == "NEEDS_CONFIRMATION":
            token = (result.data or {}).get("confirmation_token")
            self.pending = {"id": str(result.request_id), "token": token, "at": time.monotonic(), "describe": describe}
            outcome["message"] = (f"This needs the user's explicit confirmation: {describe}. Ask them to say yes or no, "
                                  "then call confirm_pending_action or cancel_pending_action.")
        elif result.data:
            outcome["data"] = _trim({k: v for k, v in result.data.items() if k != "confirmation_token"})
        if result.error and state != "DONE":
            outcome["error"] = result.error[:300]
        self._remember(describe, state)
        return outcome

    def _remember(self, describe: str, state: str):
        self.recent.append({"action": describe, "result": state, "at": time.strftime("%H:%M:%S")})

    async def call(self, name: str, args: dict) -> dict:
        return await getattr(self, name)(**args)

    # ---- context ------------------------------------------------------------
    async def desktop_context(self) -> dict:
        now = time.monotonic()
        if self._ctx_cache and now - self._ctx_cache[0] < 3:
            active = self._ctx_cache[1]
        else:
            out = await self._submit("desktop_control", "inspect_current_window", {"include_controls": False},
                                     describe="inspect the active window")
            active = out
            self._ctx_cache = (now, out)
        return {"active_window": active, "recent_actions": list(self.recent),
                "note": "Desktop is inspected only when asked; no continuous screenshots are taken."}

    async def _active_is_mt5(self) -> bool:
        ctx = await self.desktop_context()
        return bool(_MT5_WINDOW.search(json.dumps(ctx.get("active_window", {}), default=str)))

    # ---- low-risk actions -----------------------------------------------------
    async def desktop_open_app(self, target: str = "") -> dict:
        return await self._submit("windows_app", "launch", {"target": target}, describe=f"open {target}")

    async def desktop_focus_window(self, target: str = "") -> dict:
        return await self._submit("windows_app", "focus", {"target": target}, describe=f"switch to {target}")

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
        turns = getattr(self.state, "turn_controller", None)
        if turns is None:
            return {"status": "FAILED", "summary": "The assistant backend is not running"}
        answer = ""
        try:
            async with asyncio.timeout(60):
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

    # ---- human confirmation gate ------------------------------------------------
    async def confirm_pending_action(self) -> dict:
        pending = self.pending
        if not pending:
            return {"status": "NOT_FOUND", "summary": "There is no action waiting for confirmation."}
        heard, heard_at = self.last_user()
        if time.monotonic() - pending["at"] > 110 or heard_at < pending["at"]:
            return {"status": "NEEDS_CONFIRMATION", "summary": "The user has not answered yet. Ask them to say yes or no."}
        if _NEGATIVE.search(heard) or not _AFFIRMATIVE.search(heard):
            return {"status": "NEEDS_CONFIRMATION",
                    "summary": f"The user's last words were not an explicit yes ('{heard[:60]}'). Do not run it."}
        runtime = self.state.action_runtime
        self.pending = None
        try:
            result = await asyncio.wait_for(runtime.confirm(UUID(pending["id"]), pending["token"], True), 45)
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
        pending, self.pending = self.pending, None
        if not pending:
            return {"status": "NOT_FOUND", "summary": "Nothing was waiting for confirmation."}
        try:
            result = await asyncio.wait_for(self.state.action_runtime.confirm(UUID(pending["id"]), pending["token"], True), 45)
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
        try:
            await self.state.action_runtime.confirm(UUID(pending["id"]), pending["token"], False)
        except Exception:
            pass
        self._remember(pending["describe"], "BLOCKED")
        return {"status": "DONE", "summary": "Cancelled; the action did not run."}
