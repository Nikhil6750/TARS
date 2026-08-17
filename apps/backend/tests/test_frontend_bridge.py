from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from actions.frontend_bridge import FrontendBridgeError, FrontendCommandBridge
from app.ws_manager import ConnectionManager


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def accept(self) -> None:
        pass

    async def send_text(self, payload: str) -> None:
        import json

        self.sent.append(json.loads(payload))


@pytest.mark.asyncio
async def test_dispatch_raises_when_no_client_connected():
    bridge = FrontendCommandBridge(ConnectionManager())
    with pytest.raises(FrontendBridgeError, match="No connected client"):
        await bridge.dispatch(uuid4(), "browser", "click", {}, timeout=1.0)


@pytest.mark.asyncio
async def test_dispatch_broadcasts_and_resolves_on_report():
    manager = ConnectionManager()
    ws = _FakeWebSocket()
    await manager.connect(ws)
    bridge = FrontendCommandBridge(manager)
    request_id = uuid4()

    async def _report_soon() -> None:
        await asyncio.sleep(0.01)
        assert bridge.report(
            request_id, {"success": True, "data": {"summary": "clicked", "x": 1}}
        )

    reporter = asyncio.create_task(_report_soon())
    data = await bridge.dispatch(request_id, "browser", "click", {"target": "#go"}, timeout=2.0)
    await reporter

    assert data == {"summary": "clicked", "x": 1}
    assert ws.sent[0]["type"] == "frontend_command"
    assert ws.sent[0]["request_id"] == str(request_id)
    assert ws.sent[0]["skill"] == "browser"
    assert ws.sent[0]["action"] == "click"


@pytest.mark.asyncio
async def test_dispatch_raises_on_reported_failure():
    manager = ConnectionManager()
    await manager.connect(_FakeWebSocket())
    bridge = FrontendCommandBridge(manager)
    request_id = uuid4()

    async def _report_soon() -> None:
        await asyncio.sleep(0.01)
        bridge.report(request_id, {"success": False, "error": "element not found"})

    reporter = asyncio.create_task(_report_soon())
    with pytest.raises(FrontendBridgeError, match="element not found"):
        await bridge.dispatch(request_id, "browser", "click", {}, timeout=2.0)
    await reporter


@pytest.mark.asyncio
async def test_dispatch_times_out_without_report():
    manager = ConnectionManager()
    await manager.connect(_FakeWebSocket())
    bridge = FrontendCommandBridge(manager)
    with pytest.raises(FrontendBridgeError, match="did not report"):
        await bridge.dispatch(uuid4(), "browser", "click", {}, timeout=0.05)


def test_report_returns_false_for_unknown_request():
    bridge = FrontendCommandBridge(ConnectionManager())
    assert bridge.report(uuid4(), {"success": True, "data": {}}) is False


@pytest.mark.asyncio
async def test_report_returns_false_once_already_resolved():
    manager = ConnectionManager()
    await manager.connect(_FakeWebSocket())
    bridge = FrontendCommandBridge(manager)
    request_id = uuid4()

    async def _report_twice() -> None:
        await asyncio.sleep(0.01)
        assert bridge.report(request_id, {"success": True, "data": {}}) is True
        assert bridge.report(request_id, {"success": True, "data": {}}) is False

    reporter = asyncio.create_task(_report_twice())
    await bridge.dispatch(request_id, "browser", "inspect_dom", {}, timeout=2.0)
    await reporter
