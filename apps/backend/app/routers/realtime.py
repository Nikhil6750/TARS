"""Duplex PCM transport. Bounded queues fail visibly rather than replaying stale audio."""
from __future__ import annotations

import asyncio
import json

import anyio
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

from events.core import NormalizedEvent
from voice.session import LatencyMetrics, VoiceSessionController, VoiceState
import os

from app.config import get_settings
from voice.streaming import SherpaPartialEngine, SileroStreamingVAD

router = APIRouter(tags=["realtime"])


@router.post("/api/v1/events/normalized")
async def publish_event(event: NormalizedEvent, request: Request):
    return {"accepted": await request.app.state.realtime_events.publish(event)}


@router.get("/api/v1/events/normalized/recent")
async def recent_events(request: Request):
    core = request.app.state.realtime_events
    return {"events": list(core.recent), "providers": core.providers}


@router.get("/api/v1/voice/realtime/diagnostics")
async def diagnostics(request: Request):
    metrics = getattr(request.app.state, "realtime_metrics", None)
    snapshot = metrics.snapshot() if metrics else {"latest_ms": {}, "p50_ms": {}, "sample_counts": {}}
    session = getattr(request.app.state, "realtime_session", None)
    return {**snapshot, "session": session.session_id if session else None,
            "state": session.state.value if session else "IDLE",
            "events": list(session.history) if session else [],
            "providers": session.provider_status if session else {"microphone": "DISCONNECTED"}}


@router.websocket("/api/v1/voice/realtime")
async def realtime(websocket: WebSocket):
    await websocket.accept()
    state = websocket.app.state
    voice = state.voice_providers
    # One desktop conversational owner, even if multiple webviews connect.
    if getattr(state, "realtime_session", None) is not None:
        await websocket.close(code=1013, reason="A voice session is already active")
        return
    session = None
    sender = None
    outgoing: asyncio.Queue[dict] = asyncio.Queue(maxsize=128)

    async def emit(event):
        outgoing.put_nowait(event)

    async def send():
        while True:
            await websocket.send_json(await outgoing.get())

    try:
        await asyncio.wait_for(voice.ready.wait(), 5)
        if getattr(state, "realtime_session", None) is not None:
            await websocket.close(code=1013, reason="A voice session is already active")
            return
        if voice.stt.name == "mock" or voice.tts.name == "mock":
            await websocket.send_json({"type": "provider_status", "providers": {
                "stt": "DEGRADED", "tts": "DEGRADED"},
                "detail": "Real local voice providers are not ready"})
            await websocket.close(code=1013)
            return
        vad = SileroStreamingVAD()
        if not hasattr(state, "realtime_metrics"):
            state.realtime_metrics = LatencyMetrics()
        engine = None
        model_dir = os.path.expanduser(get_settings().sherpa_model_dir)
        if SherpaPartialEngine.available(model_dir):
            engine = await asyncio.to_thread(SherpaPartialEngine, model_dir)
        session = VoiceSessionController(state.turn_controller, voice, emit, vad,
                                         metrics=state.realtime_metrics, partial_engine=engine)
        state.realtime_session = session
        sender = asyncio.create_task(send())
        await session.start()
        while True:
            # Missing capture is an honest microphone disconnect, not CONNECTED forever.
            message = await asyncio.wait_for(websocket.receive(), 5)
            if message["type"] == "websocket.disconnect":
                break
            if sender.done():
                await sender
            if message.get("bytes") is not None:
                await session.push_audio(message["bytes"])
            elif message.get("text"):
                data = json.loads(message["text"])
                if data.get("type") == "interrupt":
                    await session.interrupt()
                    await session.transition(VoiceState.USER_SPEAKING if session.stt.active else VoiceState.LISTENING)
                else:
                    await session.playback(data)
    except (WebSocketDisconnect, TimeoutError):
        pass
    except Exception:
        # STT/TTS/transport failure cannot take down the application lifespan.
        import logging
        logging.getLogger("tars.realtime").exception("realtime session disconnected")
    finally:
        # ASGI servers may cancel a disconnected handler. Finish cleanup before
        # releasing the one-session lease, including under AnyIO cancellation.
        with anyio.CancelScope(shield=True):
            try:
                if session:
                    await session.close()
            finally:
                if session and getattr(state, "realtime_session", None) is session:
                    state.realtime_session = None
                if sender:
                    sender.cancel()
                    await asyncio.gather(sender, return_exceptions=True)
        try:
            await websocket.close()
        except RuntimeError:
            pass
