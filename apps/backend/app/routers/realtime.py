"""Duplex PCM transport. Bounded queues fail visibly rather than replaying stale audio."""
from __future__ import annotations

import asyncio
import json
import time
import uuid

import anyio
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

from events.core import NormalizedEvent
from voice.gemini_live import GeminiLiveVoiceSession, TarsTools
from voice.session import LatencyMetrics, VoiceSessionController, VoiceState
import os

from app.config import get_settings
from voice.streaming import SherpaPartialEngine, SileroStreamingVAD

router = APIRouter(tags=["realtime"])


async def voice_context(state) -> str:
    """Grounding for a spoken turn: live/replay quote, next calendar event, TradingView
    state and the latest alert. Only facts TARS actually has; absent sources say so."""
    parts = []
    monitors = getattr(state, "monitors", None)
    if monitors is not None:
        try:
            status = await monitors.status()
            quote = status.get("quote")
            if quote:
                parts.append(f"{quote['symbol']} bid {quote['bid']} ask {quote['ask']} (source: {quote['source']})")
            else:
                parts.append(f"No live quote (MT5 {status['mt5']['state']})")
            nxt = status["calendar"].get("next")
            if nxt:
                parts.append(f"next high/medium-impact event: {nxt['currency']} {nxt['event']} at {nxt['at']}"
                             + (" (DEMO REPLAY)" if nxt.get("replay") else ""))
            parts.append(f"TradingView chart monitor: {status['tradingview']['state']}")
        except Exception:
            pass
    alert = recent_alert_context(state)
    head = ("[TARS live context: " + "; ".join(parts) + ". Use only these facts; say when data is missing.]") if parts else ""
    return (head + " " + alert).strip()


def recent_alert_context(state) -> str:
    """One-line context of the latest actionable alert (last 10 min) so a spoken
    follow-up like "what does that mean for EURUSD?" is grounded in it."""
    from datetime import UTC, datetime, timedelta

    core = getattr(state, "realtime_events", None)
    if core is None:
        return ""
    cutoff = datetime.now(UTC) - timedelta(minutes=10)
    for item in reversed(core.recent):
        event = item["event"]
        if item["decision"] != "IGNORE" and datetime.fromisoformat(event["timestamp"]) >= cutoff:
            return (f"[Context: TARS alert just raised, {event['source']}: {event['title']}. "
                    f"{event['summary']} The user is asking about it.]")
    return ""


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
    extra = session.diag() if session is not None and hasattr(session, "diag") else {}
    return {**snapshot, **extra, "session": session.session_id if session else None,
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
        settings = get_settings()
        use_gemini = (settings.voice_provider.lower() == "gemini_live"
                      and time.monotonic() >= getattr(state, "gemini_unavailable_until", 0))
        fallback_reason = None
        if settings.voice_provider.lower() == "gemini_live" and not use_gemini:
            fallback_reason = getattr(state, "gemini_unavailable_reason", "Gemini Live unavailable")
        elif use_gemini and not settings.gemini_api_key and not getattr(state, "gemini_connect_override", None):
            use_gemini, fallback_reason = False, "GEMINI_API_KEY is not set"
        if not use_gemini:
            await asyncio.wait_for(voice.ready.wait(), 5)
        if getattr(state, "realtime_session", None) is not None:
            await websocket.close(code=1013, reason="A voice session is already active")
            return
        if not use_gemini and (voice.stt.name == "mock" or voice.tts.name == "mock"):
            await websocket.send_json({"type": "provider_status", "providers": {
                "stt": "DEGRADED", "tts": "DEGRADED"},
                "detail": "Real local voice providers are not ready"})
            await websocket.close(code=1013)
            return
        vad = SileroStreamingVAD()
        if not hasattr(state, "realtime_metrics"):
            state.realtime_metrics = LatencyMetrics()
        if use_gemini:
            session = GeminiLiveVoiceSession(
                TarsTools(state, uuid.uuid4().hex), emit, vad, model=settings.gemini_live_model, voice=getattr(settings, "gemini_live_voice", "Sadaltager"),
                idle_seconds=settings.gemini_live_idle_seconds, metrics=state.realtime_metrics,
                connect=getattr(state, "gemini_connect_override", None))
            state.realtime_session = session
            sender = asyncio.create_task(send())
            await session.start()
        else:
            engine = None
            model_dir = os.path.expanduser(settings.sherpa_model_dir)
            if SherpaPartialEngine.available(model_dir):
                engine = await asyncio.to_thread(SherpaPartialEngine, model_dir)
            session = VoiceSessionController(state.turn_controller, voice, emit, vad,
                                             metrics=state.realtime_metrics, partial_engine=engine,
                                             context_provider=lambda: voice_context(state))
            state.realtime_session = session
            sender = asyncio.create_task(send())
            await session.start()
            if fallback_reason:
                # Truthful: the UI must know it is on the local fallback and why.
                await session.send("provider_status", providers=session.provider_status.copy(),
                                   voice_provider="LOCAL_STREAMING",
                                   detail=f"Using LOCAL_STREAMING voice: {fallback_reason}")
        while True:
            # Missing capture is an honest microphone disconnect, not CONNECTED forever.
            message = await asyncio.wait_for(websocket.receive(), 5)
            if message["type"] == "websocket.disconnect":
                break
            if sender.done():
                await sender
            if getattr(session, "fatal", None):
                state.gemini_unavailable_until = time.monotonic() + 120
                state.gemini_unavailable_reason = f"Gemini Live failed: {session.fatal}"
                await asyncio.sleep(0.3)  # let the truthful status event flush
                break
            if message.get("bytes") is not None:
                await session.push_audio(message["bytes"])
            elif message.get("text"):
                data = json.loads(message["text"])
                if data.get("type") == "mute" and hasattr(session, "set_muted"):
                    await session.set_muted(bool(data.get("muted")))
                elif data.get("type") == "wake" and hasattr(session, "wake"):
                    await session.wake()
                elif data.get("type") == "confirm_action" and hasattr(session, "ui_confirm"):
                    await session.ui_confirm(bool(data.get("approve")))
                elif data.get("type") == "interrupt":
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
