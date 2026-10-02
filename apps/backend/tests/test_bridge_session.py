"""End-to-end tests of the TARS Browser Bridge's Python side -- a REAL
`BridgeServer` (real localhost websocket, real token/origin handshake) and
a `BridgeSession`/`BrowserProvider` driving it, against a small fake
"extension" client that plays the other end of the protocol
(`browser/extension/background.js`'s real dispatch table, in Python, just
enough to prove the request/response contract and the shared ordinal/
relative-reference logic from `browser/resolution.py` work against this
transport exactly as they do against CDP).

Unlike `test_browser_live_acceptance.py`, this needs no real Chrome install
and runs by default -- it is testing TARS's own server and protocol code,
not a real browser's DOM.
"""
from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from browser.bridge_server import BridgeNotConnected, BridgeServer
from browser.bridge_session import BridgeSession
from browser.provider import BrowserProvider


async def _free_port() -> int:
    server = await websockets.serve(lambda ws: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    return port


class _FakeExtension:
    """Plays just enough of background.js's ACTIONS table to exercise the
    protocol and BridgeSession's own logic -- not a reimplementation of the
    real DOM resolution (that only a real Chrome can prove, see the live
    acceptance suite), but a scripted responder a test configures per call."""

    def __init__(self, token: str, port: int, *, origin: str = "chrome-extension://fake0123456789"):
        self.token = token
        self.port = port
        self.origin = origin
        self.responses: dict[str, object] = {}
        self._ws = None
        self._task: asyncio.Task | None = None

    async def connect(self) -> None:
        self._ws = await websockets.connect(f"ws://127.0.0.1:{self.port}/bridge", extra_headers={"Origin": self.origin})
        await self._ws.send(json.dumps({"type": "hello", "token": self.token}))
        await self._ws.recv()  # hello_ack
        self._task = asyncio.create_task(self._serve())

    async def _serve(self) -> None:
        try:
            async for raw in self._ws:
                cmd = json.loads(raw)
                action = cmd["action"]
                responder = self.responses.get(action)
                result = responder(cmd.get("args") or {}) if callable(responder) else (responder if responder is not None else {"ok": False, "reason": "UNSUPPORTED_ACTION"})
                await self._ws.send(json.dumps({"type": "result", "id": cmd["id"], "result": result}))
        except websockets.exceptions.ConnectionClosed:
            pass

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
        if self._ws:
            await self._ws.close()


@pytest.fixture
async def bridge():
    port = await _free_port()
    server = BridgeServer(port=port, token="test-token-123")
    await server.start()
    yield server
    await server.stop()


async def test_bridge_session_get_context_round_trip(bridge):
    ext = _FakeExtension("test-token-123", bridge.port)
    ext.responses["get_context"] = {"url": "https://example.com", "title": "Example", "tab_count": 1, "active_tab_index": 0, "active_tab_id": "1"}
    await ext.connect()
    try:
        session = BridgeSession(bridge)
        ctx = await session.get_context()
        assert ctx["url"] == "https://example.com"
    finally:
        await ext.close()


async def test_bridge_session_ordinal_click_sends_correct_args(bridge):
    ext = _FakeExtension("test-token-123", bridge.port)
    received = {}

    def resolve_responder(args):
        received.update(args)
        return {"ok": True, "matched": {"tag": "a", "href": "https://x/1", "text": "First"},
                "diagnostics": {"matches": 5, "group": "(any)", "ordinal": 0}}

    ext.responses["get_context"] = {"url": "https://x", "title": "X"}
    ext.responses["resolve"] = resolve_responder
    await ext.connect()
    try:
        session = BridgeSession(bridge)
        out = await session.click("the first result")
        assert out["ok"] is True
        assert received["js_action"] == "click"
        assert received["args"]["ordinal"] == {"index": 0, "hint": None}
    finally:
        await ext.close()


async def test_bridge_session_relative_reference_reuses_last_ordinal(bridge):
    ext = _FakeExtension("test-token-123", bridge.port)
    calls = []

    def resolve_responder(args):
        calls.append(args)
        idx = args["args"].get("ordinal", {}).get("index", 0)
        return {"ok": True, "matched": {"tag": "a", "href": f"https://x/{idx}", "text": f"Item {idx}"},
                "diagnostics": {"matches": 5, "group": "(any)", "ordinal": idx}}

    ext.responses["get_context"] = {"url": "https://x", "title": "X"}
    ext.responses["resolve"] = resolve_responder
    await ext.connect()
    try:
        session = BridgeSession(bridge)
        first = await session.find("the first result")
        nxt = await session.find("the next one")
        assert first["matched"]["text"] == "Item 0"
        assert nxt["matched"]["text"] == "Item 1"
        assert calls[1]["args"]["ordinal"]["index"] == 1
    finally:
        await ext.close()


async def test_bridge_session_not_found_gets_bounded_recovery_retry(bridge):
    ext = _FakeExtension("test-token-123", bridge.port)
    attempt_count = {"n": 0}

    def resolve_responder(args):
        attempt_count["n"] += 1
        if attempt_count["n"] == 1:
            return {"ok": False, "reason": "NOT_FOUND", "candidates": []}
        return {"ok": True, "matched": {"tag": "button", "text": "Submit"}}

    ext.responses["get_context"] = {"url": "https://x", "title": "X"}
    ext.responses["resolve"] = resolve_responder
    await ext.connect()
    try:
        from browser import resolution

        resolution.RESOLUTION_RETRY_DELAY = 0
        session = BridgeSession(bridge)
        out = await session.click("submit")
        assert out["ok"] is True
        assert attempt_count["n"] == 2
    finally:
        await ext.close()
        from browser import resolution

        resolution.RESOLUTION_RETRY_DELAY = 0.5


async def test_browser_provider_prefers_connected_bridge_over_cdp(bridge):
    from unittest.mock import AsyncMock

    ext = _FakeExtension("test-token-123", bridge.port)
    ext.responses["get_context"] = {"url": "https://bridge-page", "title": "Via Bridge"}
    await ext.connect()
    try:
        fake_cdp = type("FakeCDP", (), {"get_context": AsyncMock(return_value={"url": "https://cdp-page"}),
                                        "aclose": AsyncMock()})()
        provider = BrowserProvider(cdp_session=fake_cdp, bridge_server=bridge)
        ctx = await provider.get_context()
        assert ctx["url"] == "https://bridge-page"
        assert provider.active_provider_name == "extension"
        fake_cdp.get_context.assert_not_called()
    finally:
        await ext.close()


async def test_browser_provider_falls_back_to_cdp_when_no_bridge(bridge):
    from unittest.mock import AsyncMock

    fake_cdp = type("FakeCDP", (), {"get_context": AsyncMock(return_value={"url": "https://cdp-page"}),
                                    "aclose": AsyncMock()})()
    provider = BrowserProvider(cdp_session=fake_cdp, bridge_server=bridge)
    ctx = await provider.get_context()
    assert ctx["url"] == "https://cdp-page"
    assert provider.active_provider_name == "cdp"


async def test_bridge_call_raises_not_connected_with_no_extension(bridge):
    with pytest.raises(BridgeNotConnected):
        await bridge.call("get_context")
