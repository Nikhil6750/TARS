"""Live backend end-to-end voice timing over the real duplex WebSocket.

Acts as the native client: streams 16 kHz PCM16 frames at real-time pace (synthetic
speech from Pocket TTS, so NOT a microphone test), acks playback like the webview does,
and barges in while TARS is speaking. Real Silero VAD, sherpa partials, faster-whisper
final, Claude and Pocket TTS all run in the backend. Stop the desktop app first (the
backend allows one voice session): taskkill /F /IM tars-companion.exe

python tools/realtime_e2e_ws.py
"""
import asyncio
import json
import time

import numpy as np
import websockets

URL = "ws://127.0.0.1:8000/api/v1/voice/realtime"
FRAME = 512  # samples (32 ms)


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


async def main():
    first = speech("TARS, what is happening with EURUSD?")
    second = speech("Stop. What about gold?")
    silence = bytes(FRAME * 2)
    t0 = time.perf_counter()
    marks: dict[str, float] = {}
    log: list[tuple[float, str, str]] = []
    LEAD = 160  # ~5 s of silence: let the session finish warming (sherpa/whisper) like a live mic would
    queue: list[bytes] = [silence] * LEAD + [first[i:i + FRAME * 2].ljust(FRAME * 2, b"\0") for i in range(0, len(first), FRAME * 2)]
    first_end = len(queue)
    queue += [silence] * 800
    second_frames = [second[i:i + FRAME * 2].ljust(FRAME * 2, b"\0") for i in range(0, len(second), FRAME * 2)]
    state = {"barged": False, "barge_index": None, "sent": 0}
    async with websockets.connect(URL, max_size=None) as ws:
        async def sender():
            i = 0
            while i < len(queue) and time.perf_counter() - t0 < 100:
                await asyncio.sleep(max(0, t0 + i * 0.032 - time.perf_counter()))
                if i == first_end:
                    marks["speech1_end"] = time.perf_counter() - t0
                if state["barged"] and state["barge_index"] is None:
                    state["barge_index"] = i
                    marks["speech2_start"] = time.perf_counter() - t0
                if state["barge_index"] is not None and i - state["barge_index"] < len(second_frames):
                    frame = second_frames[i - state["barge_index"]]
                    if i - state["barge_index"] == len(second_frames) - 1:
                        marks["speech2_end"] = time.perf_counter() - t0
                else:
                    frame = queue[i]
                await ws.send(frame)
                i += 1

        async def receiver():
            acked = set()
            async for raw in ws:
                now = time.perf_counter() - t0
                ev = json.loads(raw)
                t = ev["type"]
                detail = ev.get("state") or (ev.get("text") or "")[:60]
                if t == "delta" and sum(1 for x in log if x[1] == "delta") < 12:
                    log.append((round(now, 2), "delta", (ev.get("text") or "")[:50]))
                if t not in ("delta", "metrics"):
                    log.append((round(now, 2), t, detail or str(ev.get("providers", "")) or ev.get("detail", "")))
                marks.setdefault(t, now)
                if t == "final_transcript":
                    marks.setdefault(f"final{sum(1 for x in log if x[1] == 'final_transcript')}", now)
                if t == "audio_chunk":
                    await ws.send(json.dumps({"type": "audio_started", "turn_id": ev["turn_id"], "sequence": ev["sequence"]}))
                    if not state["barged"] and marks["audio_chunk"] < now:
                        pass
                    if "first_audio_at" not in marks:
                        marks["first_audio_at"] = now
                    acked.add((ev["turn_id"], ev["sequence"]))
                    if not state["barged"] and now - marks["first_audio_at"] > 0.6:
                        state["barged"] = True
                if t == "interrupt":
                    if state["barged"]:
                        marks.setdefault("interrupt_evt", now)
                    await ws.send(json.dumps({"type": "playback_stopped", "turn_id": ev["previous_turn_id"]}))
                if t == "first_token" or (t == "delta" and "first_delta" not in marks):
                    marks.setdefault("first_delta", now)
        tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
        await asyncio.wait([tasks[0]], timeout=105)
        await asyncio.sleep(2)
        for t in tasks:
            t.cancel()
    print("timeline:")
    for row in log:
        print(" ", row)
    print("marks:", {k: round(v, 2) for k, v in marks.items()})
    s = marks
    def d(a, b):
        return round((s[b] - s[a]) * 1000) if a in s and b in s else None
    print("\nMEASURED (ms, client clock):")
    print(" speech start -> first partial:", round((s["partial_transcript"] - LEAD * 0.032) * 1000) if "partial_transcript" in s else None)
    print(" speech end -> final transcript:", d("speech1_end", "final_transcript"))
    print(" speech end -> first delta (Claude first token):", d("speech1_end", "first_delta"))
    print(" speech end -> first audio chunk sent:", d("speech1_end", "first_audio_at"))
    print(" barge-in speech start -> interrupt event:", d("speech2_start", "interrupt_evt"))
    print(" second final transcript at:", s.get("final2"))


asyncio.run(main())
