from __future__ import annotations

import asyncio
from types import SimpleNamespace

from tests.test_gemini_live import FakeConnect, content, make, msg, speak, wait_for
from voice.session import VoiceState


async def test_muted_frames_reach_nothing_and_never_open_gemini():
    events, connect = [], FakeConnect()
    s = make(connect, events)
    await s.start()
    await s.set_muted(True)
    frames_before, speech_before = s.mic["frames"], s.mic["speech_frames"]
    await speak(s, 40)  # loud speech while muted
    assert connect.count == 0 and s.provider_status["gemini_live"] == "IDLE"
    assert s.mic["frames"] == frames_before and s.mic["speech_frames"] == speech_before  # no VAD, no accounting
    assert s.mic["sent_to_gemini"] == 0
    assert s.mic_health() == "MUTED" and s.provider_status["microphone"] == "MUTED"
    await s.close()


async def test_open_session_gets_no_audio_after_mute():
    events, connect = [], FakeConnect()
    s = make(connect, events)
    await s.start()
    await speak(s, 12)
    assert await wait_for(lambda: connect.count == 1)
    sent = connect.live.sent_audio
    await s.set_muted(True)
    await speak(s, 30)
    await asyncio.sleep(0.05)
    assert connect.live.sent_audio == sent
    await s.close()


async def test_unmute_restores_listening_without_restart():
    events, connect = [], FakeConnect()
    s = make(connect, events)
    await s.start()
    await s.set_muted(True)
    await speak(s, 20)
    assert connect.count == 0
    await s.set_muted(False)
    assert s.mic_health() in ("STARTING", "CONNECTED") and s.provider_status["microphone"] != "MUTED"
    await speak(s, 14)
    assert await wait_for(lambda: connect.count == 1 and s.provider_status["gemini_live"] == "CONNECTED")
    assert s.mic["frames"] > 0
    await s.close()


async def test_mute_mid_utterance_cancels_cleanly_and_never_sends_the_partial_turn():
    events, connect = [], FakeConnect()
    s = make(connect, events)
    await s.start()
    await speak(s, 12)
    await wait_for(lambda: connect.count == 1)
    await connect.live.script.put(msg(server_content=content(input_transcription=SimpleNamespace(text="What is happening with"))))
    assert await wait_for(lambda: s._user_open)
    sent = connect.live.sent_audio
    await s.set_muted(True)
    assert s.provider_status["gemini_live"] == "IDLE" and s._live is None  # session closed: nothing more can be answered
    assert not s._user_open and not s.stt.active and s.state is VoiceState.LISTENING
    await speak(s, 20)
    assert connect.live.sent_audio == sent
    assert not any(e["type"] == "final_transcript" for e in events)  # the unfinished utterance was never finalised
    await s.close()


async def test_assistant_audio_continues_while_muted():
    events, connect = [], FakeConnect()
    s = make(connect, events)
    await s.start()
    await speak(s, 12)
    await wait_for(lambda: connect.count == 1)
    await s.set_muted(True)
    # session may still be open when not mid-utterance: assistant output keeps flowing
    if s._live is not None:
        from tests.test_gemini_live import audio_part
        await connect.live.script.put(msg(server_content=content(model_turn=SimpleNamespace(parts=[audio_part()]))))
        assert await wait_for(lambda: any(e["type"] == "audio_pcm" for e in events))
    await s.close()


async def test_wake_click_does_not_open_gemini_while_muted_and_state_is_reported():
    events, connect = [], FakeConnect()
    s = make(connect, events)
    await s.start()
    await s.set_muted(True)
    await s.wake()
    await asyncio.sleep(0.05)
    assert connect.count == 0
    status = [e for e in events if e["type"] == "provider_status" and e.get("microphone_muted") is True]
    assert status and status[-1]["providers"]["microphone"] == "MUTED"
    assert s.diag()["microphone_muted"] is True
    await s.set_muted(False)
    assert s.diag()["microphone_muted"] is False
    await s.close()


def test_router_mute_message_is_applied_to_the_session(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routers import realtime

    opened = {"n": 0}

    def connect():
        opened["n"] += 1
        raise OSError("must not connect while muted")

    monkeypatch.setattr(realtime, "SileroStreamingVAD", lambda: lambda pcm: pcm[0] != 0)
    monkeypatch.setattr(realtime, "get_settings", lambda: SimpleNamespace(
        voice_provider="gemini_live", gemini_api_key="k", gemini_live_model="m", gemini_live_idle_seconds=30.0,
        sherpa_model_dir=""))
    app = FastAPI()
    app.state.voice_providers = SimpleNamespace(stt=SimpleNamespace(name="x"), tts=SimpleNamespace(name="y"), ready=asyncio.Event())
    app.state.turn_controller = SimpleNamespace()
    app.state.gemini_connect_override = connect
    app.include_router(realtime.router)
    with TestClient(app) as client, client.websocket_connect("/api/v1/voice/realtime") as ws:
        ws.send_text('{"type":"mute","muted":true}')
        for _ in range(30):
            ws.send_bytes(bytes([1, 0]) * 512)
        ws.send_text('{"type":"wake"}')
        diag = client.get("/api/v1/voice/realtime/diagnostics").json()
        assert diag["microphone_muted"] is True and diag["mic"]["frames"] == 0
        assert opened["n"] == 0
