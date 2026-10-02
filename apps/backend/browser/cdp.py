"""Minimal Chrome DevTools Protocol (CDP) transport.

No playwright/selenium dependency -- `httpx` (HTTP /json/* endpoints) and
`websockets` (per-target debugger websocket) are already core backend
dependencies (see requirements.txt) and are everything CDP needs: a JSON-RPC
style `{id, method, params}` request over one websocket per tab/target, plus
a handful of plain HTTP endpoints for target lifecycle (list/new/close/
activate). This module only speaks that protocol; `browser/session.py` is
where TARS-specific policy (launch, verification, element resolution) lives.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
from typing import Any
from urllib.parse import quote

import httpx
import websockets

logger = logging.getLogger("tars.browser.cdp")

_DEFAULT_TIMEOUT = 10.0


class CDPError(Exception):
    """Raised for any CDP transport/protocol failure. Callers (browser/session.py)
    turn this into a truthful FAILED ActionResult -- never a fabricated success."""


class CDPBrowser:
    """HTTP side of CDP: target discovery and lifecycle against the browser's
    `--remote-debugging-port` endpoint.

    Holds one lazily-created `httpx.AsyncClient` for its lifetime instead of
    one per call -- measured live on this machine, opening a fresh client
    (TCP connect + httpx's own setup) per call added roughly 1-4s to every
    single browser action (see the mission report's latency numbers), which
    is significant against the mission's own speed goal for what is, after
    the first call, just a localhost round trip. `aclose()` releases it;
    callers that outlive a single request (BrowserSession, held for the
    process lifetime) do not need to call it."""

    def __init__(self, port: int, host: str = "127.0.0.1") -> None:
        self.port = port
        self.host = host
        self._base = f"http://{host}:{port}"
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient()
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def version(self, *, timeout: float = 2.0) -> dict[str, Any] | None:
        try:
            resp = await self._get_client().get(f"{self._base}/json/version", timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError):
            return None

    async def list_targets(self, *, timeout: float = _DEFAULT_TIMEOUT) -> list[dict[str, Any]]:
        resp = await self._get_client().get(f"{self._base}/json/list", timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    async def new_tab(self, url: str, *, timeout: float = _DEFAULT_TIMEOUT) -> dict[str, Any]:
        resp = await self._get_client().put(f"{self._base}/json/new?{quote(url, safe='')}", timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    async def close_tab(self, target_id: str, *, timeout: float = _DEFAULT_TIMEOUT) -> None:
        resp = await self._get_client().get(f"{self._base}/json/close/{target_id}", timeout=timeout)
        resp.raise_for_status()

    async def activate_tab(self, target_id: str, *, timeout: float = _DEFAULT_TIMEOUT) -> None:
        resp = await self._get_client().get(f"{self._base}/json/activate/{target_id}", timeout=timeout)
        resp.raise_for_status()


class CDPTarget:
    """Websocket side of CDP: a connection to a single target's debugger URL.

    Held open and reused across calls (lazily connected on first use, one
    `asyncio.Lock`-guarded reconnect on a dead socket) rather than opened and
    closed per call -- measured live on this machine, reconnecting every
    call added roughly 1-2s to every find/click/type/extract (websocket
    handshake cost on top of the httpx-client cost `CDPBrowser` had the same
    problem with), which is significant against the mission's own speed
    goal. `BrowserSession` owns one `CDPTarget` per open tab and discards it
    when that tab closes or navigates to a point where the old connection is
    no longer valid (Chrome closes the debugger websocket if the target is
    destroyed, surfaced here as a `CDPError` on the next `send()`, which
    triggers exactly one reconnect-and-retry before giving up honestly)."""

    def __init__(self, ws_url: str) -> None:
        self._ws_url = ws_url
        self._ids = itertools.count(1)
        self._ws = None
        self._lock = asyncio.Lock()

    async def _ensure_connected(self, timeout: float) -> Any:
        if self._ws is not None and not self._ws.close_code:
            return self._ws
        async with self._lock:
            if self._ws is not None and not self._ws.close_code:
                return self._ws
            self._ws = await websockets.connect(self._ws_url, max_size=2**24, open_timeout=timeout)
            return self._ws

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def send(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = _DEFAULT_TIMEOUT
    ) -> dict[str, Any]:
        ws = await self._ensure_connected(timeout)
        try:
            return await self._send_on(ws, method, params, timeout=timeout)
        except (CDPError, websockets.exceptions.ConnectionClosed):
            # One honest retry against a fresh connection -- covers the tab
            # having navigated/closed and Chrome dropping the old socket
            # between calls; a second failure is reported as a real error,
            # never silently swallowed into a fabricated result.
            await self.close()
            ws = await self._ensure_connected(timeout)
            return await self._send_on(ws, method, params, timeout=timeout)

    async def send_many(
        self, calls: list[tuple[str, dict[str, Any] | None]], *, timeout: float = _DEFAULT_TIMEOUT
    ) -> list[dict[str, Any]]:
        """Sends several commands over the held connection, in order. Used
        when a later command's correctness depends on an earlier one already
        having landed on the same socket (e.g. Page.enable before
        Page.navigate)."""
        ws = await self._ensure_connected(timeout)
        results: list[dict[str, Any]] = []
        for method, params in calls:
            results.append(await self._send_on(ws, method, params, timeout=timeout))
        return results

    async def _send_on(
        self, ws, method: str, params: dict[str, Any] | None, *, timeout: float
    ) -> dict[str, Any]:
        call_id = next(self._ids)
        payload = {"id": call_id, "method": method, "params": params or {}}
        await ws.send(json.dumps(payload))
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise CDPError(f"timed out waiting for response to {method}")
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except TimeoutError as exc:
                raise CDPError(f"timed out waiting for response to {method}") from exc
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            # Events (no "id") and responses to earlier/unrelated calls on this
            # socket are both possible; only the matching id is our answer.
            if message.get("id") != call_id:
                continue
            if "error" in message:
                raise CDPError(f"{method} failed: {message['error']}")
            return message.get("result", {})


async def evaluate(target: CDPTarget, expression: str, *, timeout: float = _DEFAULT_TIMEOUT) -> Any:
    """Runs `expression` in the target's page context and returns the
    JSON-serializable value it evaluates to. Raises CDPError on a JS
    exception rather than returning it silently."""
    result = await target.send(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True, "awaitPromise": True},
        timeout=timeout,
    )
    if result.get("exceptionDetails"):
        detail = result["exceptionDetails"]
        text = detail.get("exception", {}).get("description") or detail.get("text") or "unknown JS error"
        raise CDPError(f"page script failed: {text}")
    return result.get("result", {}).get("value")
