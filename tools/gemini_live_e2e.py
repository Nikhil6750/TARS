"""Gemini Live end-to-end over the real backend WebSocket (real Gemini, synthetic speech).

Streams 16 kHz PCM16 frames at real-time pace like the native mic capture does, and reports
transcripts, audio chunks, tool calls, interruptions and latency. Synthetic (Pocket TTS) speech
is NOT a physical-microphone test. Stop the desktop app first (one voice session per backend):
    taskkill /F /IM tars-companion.exe
python tools/gemini_live_e2e.py
"""
import asyncio
import base64
import os
import json
import time

import numpy as np
import websockets

URL = "ws://127.0.0.1:8000/api/v1/voice/realtime"
FRAME = 512


def speech(text: str) -> bytes:
    import torch
    from pocket_tts import TTSModel

    torch.set_num_threads(2)
    m = TTSModel.load_model()
    state = m.get_state_for_audio_prompt("alba")
    a = np.concatenate([c.detach().cpu().numpy().reshape(-1) for c in m.generate_audio_stream(state, text)])
    n = int(len(a) * 16000 / m.sample_rate)
    a = np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a)
    return (np.clip(a, -1, 1) * 24000).astype("<i2").tobytes()


def frames(pcm: bytes):
    return [pcm[i:i + FRAME * 2].ljust(FRAME * 2, b"\0") for i in range(0, len(pcm), FRAME * 2)]


async def main():
    deep = os.environ.get("DEEP") == "1"  # ask for deep analysis (expects ask_claude), no barge-in
    prompts = [speech("TARS, can you hear me?"),
               speech("Give me a detailed technical analysis of the gold outlook this week and the key risks.") if deep else speech("What is happening with EURUSD?"),
               speech("Stop. What about gold?")]
    silence = bytes(FRAME * 2)
    t0 = time.perf_counter()
    log, marks = [], {}
    audio_bytes = {"n": 0}
    queue: asyncio.Queue = asyncio.Queue()
    stop = asyncio.Event()

    async def sender(ws):
        i = 0
        while not stop.is_set():
            await asyncio.sleep(max(0, t0 + i * 0.032 - time.perf_counter()))
            try:
                frame = queue.get_nowait()
            except asyncio.QueueEmpty:
                frame = silence
            await ws.send(frame)
            i += 1

    def say(idx, label):
        marks[label + "_sent_at"] = time.perf_counter() - t0
        for f in frames(prompts[idx]):
            queue.put_nowait(f)
        marks[label + "_end_at"] = time.perf_counter() - t0 + len(frames(prompts[idx])) * 0.032

    async with websockets.connect(URL, max_size=None) as ws:
        send_task = asyncio.create_task(sender(ws))
        await asyncio.sleep(1.0)
        say(0, "hear")
        phase = {"n": 1, "barged": False}
        async for raw in ws:
            now = time.perf_counter() - t0
            ev = json.loads(raw)
            t = ev["type"]
            if t == "audio_pcm":
                audio_bytes["n"] += len(base64.b64decode(ev["audio"]))
                marks.setdefault(f"first_audio_{phase['n']}", now)
                if not deep and phase["n"] == 2 and not phase["barged"] and now - marks["first_audio_2"] > 1.0:
                    phase["barged"] = True
                    say(2, "stop_gold")
                    marks["barge_sent_at"] = now
                continue
            if t in ("delta", "metrics"):
                continue
            detail = ev.get("text") or ev.get("state") or ev.get("name") or ev.get("detail") or ""
            if t == "provider_status":
                detail = f"{ev.get('voice_provider')} {ev.get('providers')} {ev.get('detail', '')}"
            if t == "response_complete":
                detail = ev["response"]["display_text"][:200]
            log.append((round(now, 2), t, detail[:220])); print("EV", log[-1], flush=True)
            if t == "tool_call" and ev.get("name") == "ask_claude":
                marks["ask_claude_called"] = now
            if t == "final_transcript":
                marks.setdefault(f"final_{len([1 for r in log if r[1] == 'final_transcript'])}", now)
            if t == "response_complete":
                if phase["n"] == 1:
                    phase["n"] = 2
                    await asyncio.sleep(1.0)
                    say(1, "eurusd")
                elif deep and "ask_claude_called" not in marks:
                    pass
                elif deep or phase["barged"]:
                    marks["last_response_complete"] = now
                    if "last_response_complete" in marks and len([1 for r in log if r[1] == "response_complete"]) >= 3:
                        break
            if now > 150:
                break
        stop.set()
        send_task.cancel()
    print("timeline:")
    for row in log:
        print(" ", row)
    print("marks:", {k: round(v, 2) for k, v in marks.items()})
    print("audio bytes received:", audio_bytes["n"])
    if "hear_end_at" in marks and "first_audio_1" in marks:
        print("TRUTH: speech-end -> first audio (turn 1, ms):", round((marks["first_audio_1"] - marks["hear_end_at"]) * 1000))
    if "eurusd_end_at" in marks and "first_audio_2" in marks:
        print("TRUTH: speech-end -> first audio (turn 2, tool question, ms):", round((marks["first_audio_2"] - marks["eurusd_end_at"]) * 1000))


asyncio.run(main())
