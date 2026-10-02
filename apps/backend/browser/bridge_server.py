"""TARS Browser Bridge server -- the localhost side of mission sections 4-6.

Lets the TARS Browser Bridge Chrome extension (`browser/extension/`) control
the person's *normal*, already-signed-in Chrome, as an alternative to the
dedicated-automation-profile CDP path (`browser/session.py`, which always
stays available as the fallback -- see `browser/provider.py`).

Security, all three of mission section 6's requirements:
  * localhost only -- `websockets.serve` binds `127.0.0.1`, never `0.0.0.0`.
  * random per-install token -- `ensure_token()` generates one with
    `secrets.token_urlsafe` the first time this runs and persists it under
    the user's own profile directory; nothing short of local filesystem
    access to this exact machine/account can read it. The person copies it
    into the extension's options page once (see that page's own
    instructions) -- a manual step, not an automatic handshake, which is
    the point: no website or other process can complete it without the
    person's participation.
  * origin validation + authenticated handshake -- `_handle()` requires
    BOTH the correct token AND an `Origin` header starting with
    `chrome-extension://` on the very first message, closing the
    connection otherwise. A website's page JS can open a raw WebSocket to
    `ws://127.0.0.1:<port>` (browsers do not block that), but it cannot
    forge the Origin header (browser-set, not script-settable) and has no
    way to learn this machine-local token, so it can get this far and no
    further.

Never a generic "run arbitrary code" endpoint -- `ACTIONS` in
`browser/extension/background.js` is the complete, fixed, named capability
list; this server only ever relays one of those names plus its declared
arguments, never a JS string to evaluate.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any

import websockets

logger = logging.getLogger("tars.browser.bridge")

DEFAULT_BRIDGE_PORT = 17722
_HELLO_TIMEOUT = 5.0
_CALL_TIMEOUT = 15.0


def _token_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "TARS" / "browser_bridge" / "token.txt"


def ensure_token() -> str:
    """Returns the pairing token, generating and persisting a new random one
    on first run. The person pastes this (from this exact file) into the
    extension's options page once -- see that page's own instructions."""
    path = _token_path()
    if path.is_file():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    path.write_text(token, encoding="utf-8")
    return token


class BridgeNotConnected(Exception):
    """No paired extension is currently connected."""


class BridgeError(Exception):
    """The extension reported an error running an action."""


class BridgeServer:
    """One Chrome extension client at a time (the person's one browser) --
    a second connection replaces the first rather than trying to
    multiplex multiple browsers, which this mission does not ask for."""

    def __init__(self, port: int = DEFAULT_BRIDGE_PORT, token: str | None = None) -> None:
        self.port = port
        self._token = token or ensure_token()
        self._server: websockets.WebSocketServer | None = None
        self._client: Any = None
        self._pending: dict[int, asyncio.Future] = {}
        self._ids = itertools.count(1)

    @property
    def connected(self) -> bool:
        return self._client is not None

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await websockets.serve(self._handle, "127.0.0.1", self.port)
        logger.info("[bridge] listening on 127.0.0.1:%d", self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, websocket) -> None:
        origin = _request_header(websocket, "Origin") or ""
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=_HELLO_TIMEOUT)
            hello = json.loads(raw)
        except Exception:
            await websocket.close(code=1008, reason="handshake timeout/invalid")
            return
        if hello.get("type") != "hello" or hello.get("token") != self._token:
            logger.warning("[bridge] rejected connection: bad token (origin=%r)", origin)
            await websocket.close(code=1008, reason="invalid token")
            return
        if not origin.startswith("chrome-extension://"):
            logger.warning("[bridge] rejected connection: bad origin %r", origin)
            await websocket.close(code=1008, reason="invalid origin")
            return

        logger.info("[bridge] extension paired (origin=%s)", origin)
        self._client = websocket
        await websocket.send(json.dumps({"type": "hello_ack", "ok": True}))
        try:
            async for raw in websocket:
                self._on_message(raw)
        finally:
            if self._client is websocket:
                self._client = None
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(BridgeNotConnected("the extension disconnected"))
            self._pending.clear()

    def _on_message(self, raw: str) -> None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            return
        call_id = message.get("id")
        fut = self._pending.pop(call_id, None)
        if fut is None or fut.done():
            return
        if message.get("type") == "error":
            fut.set_exception(BridgeError(str(message.get("error"))))
        else:
            fut.set_result(message.get("result"))

    async def call(self, action: str, args: dict[str, Any] | None = None, *, timeout: float = _CALL_TIMEOUT) -> Any:
        if self._client is None:
            raise BridgeNotConnected("no paired browser extension is connected")
        call_id = next(self._ids)
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[call_id] = fut
        try:
            await self._client.send(json.dumps({"type": "cmd", "id": call_id, "action": action, "args": args or {}}))
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(call_id, None)


def _request_header(websocket, name: str) -> str | None:
    headers = getattr(websocket, "request_headers", None)
    if headers is None:
        request = getattr(websocket, "request", None)
        headers = getattr(request, "headers", None)
    if headers is None:
        return None
    return headers.get(name)
