"""BridgeSession -- the BrowserAgent provider that drives the person's
*normal*, already-signed-in Chrome through the TARS Browser Bridge extension
(mission sections 4-8), instead of the dedicated-automation-profile CDP path
(`browser/session.py`'s `BrowserSession`).

Shares `browser/resolution.py`'s `ElementResolutionMixin` with
`BrowserSession` -- ordinal/relative/pronoun parsing and bounded recovery
retry are identical logic regardless of transport, so this class only
supplies the one thing that differs: how a command actually reaches the
browser (`BridgeServer.call()` over the paired extension's websocket,
instead of a CDP `Runtime.evaluate`). Every public method matches
`BrowserSession`'s name and return shape (mission section 8, "single
contract") -- `browser/provider.py` is what picks which of the two a given
request runs through.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from browser.bridge_server import BridgeServer
from browser.resolution import ElementResolutionMixin

_NAV_POLL_TIMEOUT = 12.0
_HISTORY_POLL_TIMEOUT = 8.0
_CLICK_NAV_VERIFY_TIMEOUT = 4.0
_POLL_INTERVAL = 0.3


class BridgeSession(ElementResolutionMixin):
    def __init__(self, server: BridgeServer) -> None:
        self._server = server
        self._init_resolution_state()
        self._last_download: dict[str, Any] | None = None

    @property
    def connected(self) -> bool:
        return self._server.connected

    # ---- tabs ---------------------------------------------------------------
    async def get_context(self) -> dict[str, Any]:
        return await self._server.call("get_context")

    async def list_tabs(self) -> list[dict[str, Any]]:
        return await self._server.call("list_tabs")

    async def focus_tab(self, target: str) -> dict[str, Any]:
        return await self._server.call("focus_tab", {"target": target})

    async def new_tab(self, url: str = "") -> dict[str, Any]:
        return await self._server.call("new_tab", {"url": url})

    async def close_tab(self, target: str = "") -> dict[str, Any]:
        return await self._server.call("close_tab", {"target": target})

    # ---- navigation -----------------------------------------------------------
    async def navigate(self, url: str) -> dict[str, Any]:
        out = await self._server.call("navigate", {"url": url}, timeout=_NAV_POLL_TIMEOUT + 3)
        self._forget_relative_references()
        return out

    async def back(self) -> dict[str, Any]:
        out = await self._server.call("back", {}, timeout=_HISTORY_POLL_TIMEOUT + 3)
        self._forget_relative_references()
        return out

    async def forward(self) -> dict[str, Any]:
        out = await self._server.call("forward", {}, timeout=_HISTORY_POLL_TIMEOUT + 3)
        self._forget_relative_references()
        return out

    async def refresh(self) -> dict[str, Any]:
        out = await self._server.call("refresh", {}, timeout=_NAV_POLL_TIMEOUT + 3)
        self._forget_relative_references()
        return out

    # ---- semantic element resolution + action -----------------------------
    async def find(self, target: str) -> dict[str, Any]:
        return await self._resolve_and_act("find", target)

    async def click(self, target: str) -> dict[str, Any]:
        return await self._resolve_and_act("click", target)

    async def type_text(self, target: str, text: str, *, clear: bool = True, submit: bool = False) -> dict[str, Any]:
        return await self._resolve_and_act("type", target, {"text": text, "clear": clear, "submit": submit})

    async def select(self, target: str, value: str) -> dict[str, Any]:
        return await self._resolve_and_act("select", target, {"value": value})

    async def scroll_to(self, target: str) -> dict[str, Any]:
        return await self._resolve_and_act("scroll_to", target)

    async def scroll(self, direction: str = "down", amount: str = "small") -> dict[str, Any]:
        return await self._server.call("scroll", {"direction": direction, "amount": amount})

    async def wait_for(self, target: str, timeout: float = 10.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last: dict[str, Any] = {"ok": False, "reason": "NOT_FOUND", "query": target}
        while time.monotonic() < deadline:
            last = await self.find(target)
            if last.get("ok"):
                return last
            await asyncio.sleep(_POLL_INTERVAL)
        return last

    async def _execute(self, js_action: str, js_target: str, args: dict[str, Any]) -> dict[str, Any]:
        before_ctx = await self._server.call("get_context")
        before_url = before_ctx.get("url", "")
        result = await self._server.call("resolve", {"js_action": js_action, "target": js_target, "args": args})
        self._remember_result(result)
        expects_navigation = (js_action == "click" and (result.get("matched") or {}).get("href")) or \
            (js_action == "type" and args.get("submit"))
        if result.get("ok") and expects_navigation:
            navigated = await self._poll_url_change(before_url, timeout=_CLICK_NAV_VERIFY_TIMEOUT)
            result["navigated"] = navigated
            if navigated:
                after_ctx = await self._server.call("get_context")
                result["url_after"], result["title_after"] = after_ctx.get("url"), after_ctx.get("title")
                self._forget_relative_references()
        return result

    async def _poll_url_change(self, before_url: str, *, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ctx = await self._server.call("get_context")
            if ctx.get("url") and ctx["url"] != before_url:
                return True
            await asyncio.sleep(_POLL_INTERVAL)
        return False

    # ---- read-only extraction ----------------------------------------------
    async def extract_text(self, mode: str = "summary") -> dict[str, Any]:
        return await self._server.call("extract_text", {"mode": mode})

    async def get_links(self) -> dict[str, Any]:
        return await self._server.call("get_links")

    async def extract_table(self, target: str = "") -> dict[str, Any]:
        return await self._server.call("extract_table", {"target": target})

    # ---- downloads ----------------------------------------------------------
    async def download(self, target: str, *, timeout: float = 30.0) -> dict[str, Any]:
        clicked = await self.click(target)
        if not clicked.get("ok"):
            return {"ok": False, "status": "FAILED", "reason": clicked.get("reason", "NOT_FOUND"),
                    "query": target, "candidates": clicked.get("candidates", [])}
        source_url = (clicked.get("matched") or {}).get("href")
        out = await self._server.call(
            "wait_for_download", {"source_url": source_url, "timeout_ms": int(timeout * 1000)},
            timeout=timeout + 5,
        )
        if out.get("status") == "COMPLETED":
            self._last_download = {**out, "at": time.time()}
        return out

    def get_last_download(self) -> dict[str, Any]:
        if self._last_download is None:
            return {"ok": False, "reason": "NOT_FOUND", "detail": "nothing has been downloaded yet this session"}
        return {"ok": True, **{k: v for k, v in self._last_download.items() if k != "at"}}
