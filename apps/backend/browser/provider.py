"""BrowserProvider -- picks which BrowserAgent implementation actually runs
a given action (mission sections 7-8).

Priority order, decided fresh on every call (so a mid-session pairing or
disconnect takes effect immediately, never requiring a restart):

  1. the TARS Browser Bridge extension, if a browser has paired and is
     currently connected (`browser/bridge_session.py` -- the person's own,
     already-signed-in Chrome)
  2. the dedicated-automation-profile CDP session (`browser/session.py` --
     always available, never removed; mission section 7: "do not delete
     the working dedicated-profile CDP implementation")

Exposes the exact same method surface either provider does, so
`skills/web_browser.py` calls `click`/`type_text`/`navigate`/etc. without
ever knowing or caring which one actually ran (mission section 8, "single
contract" -- "upper-level Gemini tools must not care whether execution
happened through extension/CDP/UIA"). `active_provider_name` exists only
for diagnostics (what actually served the last request), not for callers to
branch on.

Explicitly out of scope here (per the mission's own provider list: browser
UIA, vision, mouse/keyboard as further fallbacks below CDP) -- those apply
to the desktop agent generally (`skills/desktop_control.py`), not
specifically to browser actions, and already exist unchanged.
"""
from __future__ import annotations

from typing import Any

from browser.bridge_server import BridgeServer
from browser.bridge_session import BridgeSession
from browser.session import BrowserSession


class BrowserProvider:
    def __init__(self, cdp_session: BrowserSession | None = None, bridge_server: BridgeServer | None = None) -> None:
        self._cdp = cdp_session or BrowserSession()
        self._bridge_server = bridge_server or BridgeServer()
        self._bridge = BridgeSession(self._bridge_server)
        self._bridge_started = False

    async def _target(self):
        # Starting the bridge server just means listening on localhost for
        # a pairing -- cheap and inert until an extension actually connects
        # (same "do nothing until first real use" posture as the CDP
        # session's lazy Chrome launch), so this is safe to do on every
        # provider resolution rather than gating it behind some separate
        # "enable the bridge" step.
        if not self._bridge_started:
            await self._bridge_server.start()
            self._bridge_started = True
        return self._bridge if self._bridge.connected else self._cdp

    @property
    def active_provider_name(self) -> str:
        return "extension" if self._bridge.connected else "cdp"

    async def aclose(self) -> None:
        await self._cdp.aclose()
        await self._bridge_server.stop()

    # ---- tabs ---------------------------------------------------------------
    async def get_context(self) -> dict[str, Any]:
        return await (await self._target()).get_context()

    async def list_tabs(self) -> list[dict[str, Any]]:
        return await (await self._target()).list_tabs()

    async def focus_tab(self, target: str) -> dict[str, Any]:
        return await (await self._target()).focus_tab(target)

    async def new_tab(self, url: str = "") -> dict[str, Any]:
        return await (await self._target()).new_tab(url)

    async def close_tab(self, target: str = "") -> dict[str, Any]:
        return await (await self._target()).close_tab(target)

    # ---- navigation -----------------------------------------------------------
    async def navigate(self, url: str) -> dict[str, Any]:
        return await (await self._target()).navigate(url)

    async def back(self) -> dict[str, Any]:
        return await (await self._target()).back()

    async def forward(self) -> dict[str, Any]:
        return await (await self._target()).forward()

    async def refresh(self) -> dict[str, Any]:
        return await (await self._target()).refresh()

    # ---- semantic element resolution + action -----------------------------
    async def find(self, target: str) -> dict[str, Any]:
        return await (await self._target()).find(target)

    async def click(self, target: str) -> dict[str, Any]:
        return await (await self._target()).click(target)

    async def type_text(self, target: str, text: str, *, clear: bool = True, submit: bool = False) -> dict[str, Any]:
        return await (await self._target()).type_text(target, text, clear=clear, submit=submit)

    async def select(self, target: str, value: str) -> dict[str, Any]:
        return await (await self._target()).select(target, value)

    async def scroll_to(self, target: str) -> dict[str, Any]:
        return await (await self._target()).scroll_to(target)

    async def scroll(self, direction: str = "down", amount: str = "small") -> dict[str, Any]:
        return await (await self._target()).scroll(direction, amount)

    async def wait_for(self, target: str, timeout: float = 10.0) -> dict[str, Any]:
        return await (await self._target()).wait_for(target, timeout=timeout)

    # ---- read-only extraction ----------------------------------------------
    async def extract_text(self, mode: str = "summary") -> dict[str, Any]:
        return await (await self._target()).extract_text(mode)

    async def get_links(self) -> dict[str, Any]:
        return await (await self._target()).get_links()

    async def extract_table(self, target: str = "") -> dict[str, Any]:
        return await (await self._target()).extract_table(target)

    # ---- downloads ----------------------------------------------------------
    async def download(self, target: str, *, timeout: float = 30.0) -> dict[str, Any]:
        return await (await self._target()).download(target, timeout=timeout)

    def get_last_download(self) -> dict[str, Any]:
        # Not provider-selected like the rest: whichever provider actually
        # ran the last download is the one whose record is meaningful, and
        # that is not necessarily today's "active" one (the person could
        # have paired/unpaired the bridge in between) -- compare each
        # provider's own raw last-download timestamp directly rather than
        # asking only the currently-active provider, which could wrongly
        # report "nothing downloaded yet" right after a real download that
        # happened through the other one.
        bridge_raw, cdp_raw = self._bridge._last_download, self._cdp._last_download
        if bridge_raw and (not cdp_raw or bridge_raw["at"] >= cdp_raw["at"]):
            return self._bridge.get_last_download()
        if cdp_raw:
            return self._cdp.get_last_download()
        return {"ok": False, "reason": "NOT_FOUND", "detail": "nothing has been downloaded yet this session"}
