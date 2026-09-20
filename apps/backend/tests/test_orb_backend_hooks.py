from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from app.action_contracts import ActionResult, ActionStatus, RiskLevel
from tests.test_gemini_live import FakeConnect, make, msg, speak, wait_for
from voice.desktop_tools import DesktopTools
from voice.gemini_live import TarsTools


class Runtime:
    def __init__(self):
        self.confirms = []

    async def submit(self, request):
        return ActionResult(request_id=request.id, status=ActionStatus.CONFIRMATION_REQUIRED, risk_level=RiskLevel.CONFIRM_REQUIRED,
                            summary="needs confirmation", data={"confirmation_token": "TOK"})

    async def confirm(self, request_id, token, approved):
        self.confirms.append((token, approved))
        return ActionResult(request_id=request_id, status=ActionStatus.SUCCEEDED if approved else ActionStatus.DENIED, summary="ok")


async def test_tool_result_and_confirmation_events_feed_the_orb_and_ui_confirm_uses_action_runtime():
    events, connect = [], FakeConnect()
    runtime = Runtime()
    tools = TarsTools(SimpleNamespace(action_runtime=runtime), "s")
    s = make(connect, events, tools=tools)
    await s.start()
    await speak(s)
    await wait_for(lambda: connect.count == 1)
    call = SimpleNamespace(id="k1", name="desktop_click_control", args={"control_id": "b1", "label": "Save"})
    await connect.live.script.put(msg(tool_call=SimpleNamespace(function_calls=[call])))
    assert await wait_for(lambda: any(e["type"] == "confirmation_pending" for e in events))
    result = [e for e in events if e["type"] == "tool_result"][0]
    assert result["name"] == "desktop_click_control" and result["status"] == "NEEDS_CONFIRMATION"
    assert "Save" in [e for e in events if e["type"] == "confirmation_pending"][0]["text"]
    assert "TOK" not in str(events)  # the token never leaves the backend

    await s.ui_confirm(True)  # the human clicked Yes on the orb
    assert runtime.confirms == [("TOK", True)]
    assert any(e["type"] == "confirmation_cleared" for e in events)
    await s.close()


async def test_ui_no_cancels_and_stale_confirm_is_ignored():
    events, connect = [], FakeConnect()
    runtime = Runtime()
    tools = TarsTools(SimpleNamespace(action_runtime=runtime), "s")
    s = make(connect, events, tools=tools)
    await s.start()
    await tools.desktop.desktop_click_control("b", "OK")
    assert tools.desktop.pending
    await s.ui_confirm(False)
    assert runtime.confirms == [("TOK", False)] and tools.desktop.pending is None
    await s.ui_confirm(True)  # nothing pending any more
    assert len(runtime.confirms) == 1
    await s.close()


async def test_orb_wake_opens_the_session_without_speech_and_only_once():
    events, connect = [], FakeConnect()
    s = make(connect, events)
    await s.start()
    assert connect.count == 0
    await s.wake()
    await s.wake()
    assert await wait_for(lambda: connect.count == 1 and s.provider_status["gemini_live"] == "CONNECTED")
    await s.wake()
    await asyncio.sleep(0.05)
    assert connect.count == 1
    await s.close()


def test_ui_confirm_is_the_same_execution_path_as_voice_confirm():
    import inspect

    src = inspect.getsource(DesktopTools.ui_confirm)
    assert "action_runtime.confirm" in src and "_recording" not in src
    assert time  # module imported for parity with the voice-gate tests


def test_second_websocket_is_refused_while_a_session_is_active(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from app.routers import realtime

    monkeypatch.setattr(realtime, "SileroStreamingVAD", lambda: lambda pcm: False)
    monkeypatch.setattr(realtime, "get_settings", lambda: SimpleNamespace(
        voice_provider="gemini_live", gemini_api_key="k", gemini_live_model="m", gemini_live_idle_seconds=30.0,
        sherpa_model_dir=""))
    app = FastAPI()
    app.state.voice_providers = SimpleNamespace(stt=SimpleNamespace(name="x"), tts=SimpleNamespace(name="y"), ready=asyncio.Event())
    app.state.turn_controller = SimpleNamespace()
    app.state.gemini_connect_override = lambda: None
    app.include_router(realtime.router)
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/voice/realtime") as first:
            first.send_bytes(bytes(1024))
            first.receive_json()
            with client.websocket_connect("/api/v1/voice/realtime") as second:
                try:
                    second.receive_json()
                    refused = False
                except WebSocketDisconnect as exc:
                    refused = exc.code == 1013
            assert refused
            assert app.state.realtime_session.voice_provider == "GEMINI_LIVE"
