"""BrowserSession -- the BrowserAgent's single point of contact with a real
Chrome instance over CDP.

Responsibilities (deliberately all in one place, per the mission's "do not
build another desktop automation framework" -- this is the one browser
automation path, `skills/web_browser.py` is only argument validation/risk
classification/ActionResult shaping on top of it, matching every other skill
in this codebase):

  * ensure a debuggable Chrome is reachable, launching one if not (a
    dedicated TARS automation profile -- see module docstring on
    `_default_profile_dir` for why this, not the user's everyday profile)
  * track which tab is "active" for this voice session
  * navigate/tab-manage with real verification (poll actual page state,
    never assume success from a dispatched command)
  * semantic element resolution + action, via `browser/element_resolver.py`,
    in one CDP round trip per call
  * read-only extraction (text/links/table)

Every public method returns a plain dict the `web_browser` skill turns into
an ActionResult -- `{"ok": False, "reason": "NOT_FOUND"}`-shaped outcomes are
not exceptions, they are the honest, expected result of e.g. clicking
something that is not on the page; `CDPError`/`BrowserLaunchError` are for
actual transport/launch failures.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from browser.cdp import CDPBrowser, CDPError, CDPTarget, evaluate
from browser.element_resolver import build_invocation

logger = logging.getLogger("tars.browser.session")

DEFAULT_DEBUG_PORT = 9222
_NAV_TIMEOUT = 12.0
_HISTORY_TIMEOUT = 8.0
_POLL_INTERVAL = 0.3
_LAUNCH_TIMEOUT = 12.0


class BrowserLaunchError(Exception):
    """Chrome could not be found or did not become reachable on the debug port."""


def _default_profile_dir() -> Path:
    # A dedicated automation profile, not the user's everyday Chrome profile.
    # Chrome enforces one process per user-data-dir; if the user already has
    # Chrome open against their normal profile without --remote-debugging-port,
    # launching a second process pointed at that same profile would not attach
    # a debugger to it -- Chrome just focuses the existing window and ignores
    # the new flags. A separate profile is what actually gets TARS a real,
    # inspectable CDP connection without first asking the user to close their
    # browser. It does start logged out of the user's sites; this is a known,
    # documented limitation (see the mission report), not a silent gap.
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "TARS" / "chrome-automation-profile"


def _validate_http_url(raw_url: str) -> str:
    # Reuses skills.browser's validator (http/https only, real host) rather
    # than a second copy of the same scheme check.
    from skills.browser import validate_http_url

    return validate_http_url(raw_url)


class BrowserSession:
    def __init__(
        self,
        port: int = DEFAULT_DEBUG_PORT,
        profile_dir: Path | None = None,
        *,
        resolver=None,
        http: CDPBrowser | None = None,
    ) -> None:
        self.port = port
        self._profile_dir = profile_dir or _default_profile_dir()
        self._resolver = resolver
        self._http = http or CDPBrowser(port)
        self._active_target_id: str | None = None
        self._launch_lock = asyncio.Lock()
        # One held-open CDPTarget connection per tab, reused across calls --
        # see browser/cdp.py's CDPTarget docstring for why (per-call
        # reconnect measured ~1-2s of pure overhead on top of the action
        # itself). Discarded when a tab closes.
        self._connections: dict[str, CDPTarget] = {}

    # ---- lifecycle ------------------------------------------------------
    async def ensure_started(self) -> None:
        if await self._http.version(timeout=1.5) is not None:
            return
        async with self._launch_lock:
            if await self._http.version(timeout=1.5) is not None:
                return
            exe = self._find_chrome_executable()
            self._profile_dir.mkdir(parents=True, exist_ok=True)
            logger.info("[browser] launching Chrome (debug port %d, profile %s)", self.port, self._profile_dir)
            subprocess.Popen(  # noqa: S603
                [
                    exe,
                    f"--remote-debugging-port={self.port}",
                    f"--user-data-dir={self._profile_dir}",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
                close_fds=True,
            )
            deadline = time.monotonic() + _LAUNCH_TIMEOUT
            while time.monotonic() < deadline:
                if await self._http.version(timeout=1.0) is not None:
                    return
                await asyncio.sleep(0.4)
            raise BrowserLaunchError("Chrome did not become reachable on the debug port in time")

    def _find_chrome_executable(self) -> str:
        from skills.app_resolver import get_resolver

        resolver = self._resolver or get_resolver()
        result = resolver.resolve("chrome")
        if result.outcome != "MATCH" or result.app is None or not result.app.executable:
            raise BrowserLaunchError(
                "Could not find an installed Chrome executable to launch (app resolver found no match)"
            )
        return result.app.executable

    # ---- targets/tabs -----------------------------------------------------
    async def _targets(self) -> list[dict[str, Any]]:
        await self.ensure_started()
        targets = await self._http.list_targets()
        return [t for t in targets if t.get("type") == "page"]

    async def _active_target(self) -> dict[str, Any]:
        targets = await self._targets()
        if not targets:
            created = await self._http.new_tab("about:blank")
            self._active_target_id = created["id"]
            return created
        if self._active_target_id:
            for t in targets:
                if t["id"] == self._active_target_id:
                    return t
        self._active_target_id = targets[0]["id"]
        return targets[0]

    async def aclose(self) -> None:
        """Closes every held connection. Not required for correctness (CDP
        connections are cleaned up when Chrome itself exits), but avoids
        leaking open websockets/HTTP clients past a caller's own lifetime
        (used by tests; a long-lived backend process can skip this)."""
        for conn in self._connections.values():
            await conn.close()
        self._connections.clear()
        await self._http.aclose()

    def _connection_for(self, info: dict[str, Any]) -> CDPTarget:
        conn = self._connections.get(info["id"])
        if conn is None:
            conn = CDPTarget(info["webSocketDebuggerUrl"])
            self._connections[info["id"]] = conn
        return conn

    @staticmethod
    def _match_tab(targets: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
        if not query:
            return None
        query = query.strip()
        for t in targets:
            if t.get("id") == query:
                return t
        if query.isdigit():
            idx = int(query)
            if 0 <= idx < len(targets):
                return targets[idx]
        needle = query.lower()
        for t in targets:
            if needle in (t.get("title") or "").lower() or needle in (t.get("url") or "").lower():
                return t
        return None

    async def get_context(self) -> dict[str, Any]:
        targets = await self._targets()
        active = await self._active_target()
        index = next((i for i, t in enumerate(targets) if t["id"] == active["id"]), 0)
        return {
            "url": active.get("url"), "title": active.get("title"),
            "tab_count": len(targets), "active_tab_index": index, "active_tab_id": active["id"],
        }

    async def list_tabs(self) -> list[dict[str, Any]]:
        targets = await self._targets()
        active = await self._active_target()
        return [
            {"index": i, "id": t["id"], "url": t.get("url"), "title": t.get("title"), "active": t["id"] == active["id"]}
            for i, t in enumerate(targets)
        ]

    async def focus_tab(self, target: str) -> dict[str, Any]:
        targets = await self._targets()
        match = self._match_tab(targets, target)
        if match is None:
            return {"ok": False, "reason": "NOT_FOUND", "query": target}
        await self._http.activate_tab(match["id"])
        self._active_target_id = match["id"]
        return {"ok": True, "id": match["id"], "url": match.get("url"), "title": match.get("title")}

    async def new_tab(self, url: str = "") -> dict[str, Any]:
        target_url = _validate_http_url(url) if url else "about:blank"
        created = await self._http.new_tab(target_url)
        self._active_target_id = created["id"]
        return {"ok": True, "id": created["id"], "url": created.get("url"), "title": created.get("title")}

    async def close_tab(self, target: str = "") -> dict[str, Any]:
        targets = await self._targets()
        if not targets:
            return {"ok": False, "reason": "NOT_FOUND", "query": target}
        match = self._match_tab(targets, target) if target else await self._active_target()
        if match is None:
            return {"ok": False, "reason": "NOT_FOUND", "query": target}
        was_active = match["id"] == self._active_target_id
        await self._http.close_tab(match["id"])
        if was_active:
            self._active_target_id = None
        conn = self._connections.pop(match["id"], None)
        if conn is not None:
            await conn.close()
        return {"ok": True, "closed_id": match["id"]}

    # ---- navigation (verified) --------------------------------------------
    async def _poll_ready(self, target: CDPTarget, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                state = await evaluate(target, "document.readyState")
            except CDPError:
                state = None
            if state == "complete":
                return True
            await asyncio.sleep(_POLL_INTERVAL)
        return False

    async def _current_url_title(self, target: CDPTarget) -> tuple[str, str]:
        try:
            url = await evaluate(target, "document.location.href")
            title = await evaluate(target, "document.title")
        except CDPError:
            return "", ""
        return url or "", title or ""

    async def navigate(self, url: str) -> dict[str, Any]:
        validated = _validate_http_url(url)
        info = await self._active_target()
        target = self._connection_for(info)
        await target.send("Page.navigate", {"url": validated})
        ready = await self._poll_ready(target, _NAV_TIMEOUT)
        cur_url, title = await self._current_url_title(target)
        return {"ok": ready, "url": cur_url or validated, "title": title, "requested": validated}

    async def _history_nav(self, js: str) -> dict[str, Any]:
        info = await self._active_target()
        target = self._connection_for(info)
        before_url, _ = await self._current_url_title(target)
        await evaluate(target, js)
        deadline = time.monotonic() + _HISTORY_TIMEOUT
        after_url = before_url
        while time.monotonic() < deadline:
            await asyncio.sleep(_POLL_INTERVAL)
            after_url, _ = await self._current_url_title(target)
            if after_url != before_url:
                break
        ready = await self._poll_ready(target, _HISTORY_TIMEOUT)
        after_url, title = await self._current_url_title(target)
        return {"ok": ready and after_url != before_url, "url": after_url, "title": title, "changed": after_url != before_url}

    async def back(self) -> dict[str, Any]:
        return await self._history_nav("history.back()")

    async def forward(self) -> dict[str, Any]:
        return await self._history_nav("history.forward()")

    async def refresh(self) -> dict[str, Any]:
        info = await self._active_target()
        target = self._connection_for(info)
        await evaluate(target, "location.reload()")
        ready = await self._poll_ready(target, _NAV_TIMEOUT)
        url, title = await self._current_url_title(target)
        return {"ok": ready, "url": url, "title": title}

    # ---- semantic element resolution + action -----------------------------
    async def _eval_active(self, expression: str, *, timeout: float = 10.0) -> Any:
        info = await self._active_target()
        target = self._connection_for(info)
        return await evaluate(target, expression, timeout=timeout)

    async def find(self, target: str) -> dict[str, Any]:
        return await self._eval_active(build_invocation("find", target))

    async def click(self, target: str) -> dict[str, Any]:
        return await self._eval_active(build_invocation("click", target))

    async def type_text(self, target: str, text: str, *, clear: bool = True, submit: bool = False) -> dict[str, Any]:
        return await self._eval_active(build_invocation("type", target, {"text": text, "clear": clear, "submit": submit}))

    async def select(self, target: str, value: str) -> dict[str, Any]:
        return await self._eval_active(build_invocation("select", target, {"value": value}))

    async def scroll_to(self, target: str) -> dict[str, Any]:
        return await self._eval_active(build_invocation("scroll_to", target))

    async def scroll(self, direction: str = "down", amount: str = "small") -> dict[str, Any]:
        px = {"small": 400, "large": 1200}.get(amount, 400)
        delta = px if direction == "down" else -px
        if direction in ("top", "bottom"):
            js = "window.scrollTo({top: 0, behavior: 'smooth'})" if direction == "top" \
                else "window.scrollTo({top: document.body.scrollHeight, behavior: 'smooth'})"
        else:
            js = f"window.scrollBy({{top: {delta}, behavior: 'smooth'}})"
        await self._eval_active(f"(function(){{ {js}; return true; }})()")
        return {"ok": True, "direction": direction, "amount": amount}

    # ---- read-only extraction ----------------------------------------------
    async def extract_text(self, mode: str = "summary") -> dict[str, Any]:
        js = """(function(mode){
          if (mode === 'headings') {
            return Array.from(document.querySelectorAll('h1,h2,h3'))
              .map(h => h.textContent.trim()).filter(Boolean).join('\\n');
          }
          var body = document.body ? (document.body.innerText || document.body.textContent || '') : '';
          body = body.trim();
          if (mode === 'summary') {
            return body.split('\\n').filter(l => l.trim().length > 0).slice(0, 20).join('\\n');
          }
          return body.slice(0, 20000);
        })(%s)""" % json.dumps(mode)
        text = await self._eval_active(js)
        return {"ok": True, "mode": mode, "text": text or ""}

    async def get_links(self) -> dict[str, Any]:
        js = """(function(){
          var seen = {};
          return Array.from(document.querySelectorAll('a[href]'))
            .map(a => ({ text: (a.innerText || a.textContent || '').trim().slice(0, 120), href: a.href }))
            .filter(l => l.text && l.href && !seen[l.href] && (seen[l.href] = true))
            .slice(0, 40);
        })()"""
        links = await self._eval_active(js)
        return {"ok": True, "links": links or []}

    async def extract_table(self, target: str = "") -> dict[str, Any]:
        js = """(function(query){
          var table = null;
          if (query) {
            var all = Array.from(document.querySelectorAll('table'));
            table = all.find(t => (t.innerText || '').toLowerCase().includes(query.toLowerCase())) || all[0];
          } else {
            table = document.querySelector('table');
          }
          if (!table) return { ok: false, reason: 'NOT_FOUND' };
          var rows = Array.from(table.querySelectorAll('tr')).slice(0, 50).map(
            tr => Array.from(tr.querySelectorAll('th,td')).map(c => (c.innerText || '').trim())
          );
          return { ok: true, rows: rows };
        })(%s)""" % json.dumps(target)
        return await self._eval_active(js)

    async def wait_for(self, target: str, timeout: float = 10.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last: dict[str, Any] = {"ok": False, "reason": "NOT_FOUND", "query": target}
        while time.monotonic() < deadline:
            last = await self.find(target)
            if last.get("ok"):
                return last
            await asyncio.sleep(_POLL_INTERVAL)
        return last
