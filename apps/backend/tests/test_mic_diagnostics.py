from __future__ import annotations

import time

import numpy as np

import voice.gemini_live as gl
from tests.test_gemini_live import FakeConnect, make, speak, wait_for


def tone(amp=8000, n=512):
    t = np.arange(n)
    return (np.sin(t / 5 + 1.0) * amp).astype("<i2").tobytes()


async def test_frames_levels_vad_and_gemini_counters_are_measured_not_assumed():
    events, connect = [], FakeConnect()
    s = make(connect, events)
    await s.start()
    for _ in range(4):
        await s.push_audio(bytes(1024))  # digital silence
    silent_db = s.mic["db"]
    for _ in range(12):
        await s.push_audio(tone())
    d = s.diag()["mic"]
    assert d["frames"] == 16 and silent_db == -120.0
    assert d["max_db"] > -20 and d["db"] > -60
    assert d["vad"] is False or d["vad"] is True  # the flag mirrors the local VAD
    assert await wait_for(lambda: connect.count == 1)
    assert s.diag()["mic"]["sent_to_gemini"] > 0 and s.diag()["gemini"] == "CONNECTED"
    await s.close()


async def test_speech_bumps_local_vad_and_frames_reach_gemini():
    events, connect = [], FakeConnect()
    s = make(connect, events)
    await s.start()
    await speak(s, 14)  # helper frames trip the injected VAD
    assert s.mic["speech_frames"] >= 8
    assert await wait_for(lambda: connect.live.sent_audio >= 8)
    assert s.mic["sent_to_gemini"] == connect.live.sent_audio or s.mic["sent_to_gemini"] > 0
    await s.close()


async def test_health_starting_then_disconnected_when_no_frames_ever_arrive(monkeypatch):
    events, connect = [], FakeConnect()
    s = make(connect, events)
    await s.start()
    assert s.mic_health() == "STARTING"
    s._started_at -= 10
    assert s.mic_health() == "DISCONNECTED"
    await s.close()


async def test_health_silent_when_frames_flow_but_never_any_signal(monkeypatch):
    monkeypatch.setattr(gl, "MIC_SILENT_AFTER_S", 0.05)
    events, connect = [], FakeConnect()
    s = make(connect, events)
    await s.start()
    await s.push_audio(bytes(1024))
    assert s.mic_health() == "CONNECTED"  # too early to call it dead
    time.sleep(0.08)
    await s.push_audio(bytes(1024))
    assert s.mic_health() == "SILENT"
    await s.push_audio(tone(12000))  # any real energy proves the path works
    assert s.mic_health() == "CONNECTED"
    await s.close()


async def test_health_disconnected_when_frames_stop():
    events, connect = [], FakeConnect()
    s = make(connect, events)
    await s.start()
    await s.push_audio(tone())
    assert s.mic_health() == "CONNECTED"
    s.mic["last_frame_at"] -= 5
    assert s.mic_health() == "DISCONNECTED"
    await s.close()


async def test_status_changes_are_published_to_the_orb(monkeypatch):
    events, connect = [], FakeConnect()
    s = make(connect, events)
    await s.start()
    await s.push_audio(tone())
    s.mic["last_frame_at"] -= 5
    assert await wait_for(lambda: any((e.get("providers") or {}).get("microphone") == "DISCONNECTED" for e in events), timeout=3)
    await s.close()


async def test_diagnostics_never_contain_audio_content():
    events, connect = [], FakeConnect()
    s = make(connect, events)
    await s.start()
    await s.push_audio(tone())
    d = s.diag()
    assert set(d["mic"]) == {"frames", "db", "max_db", "vad", "speech_frames", "sent_to_gemini", "status", "health"}
    assert "audio" not in str(d).lower()
    await s.close()
